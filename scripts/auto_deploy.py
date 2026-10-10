"""Deploy green GitHub commits on the VPS without copying secrets or data.

This helper is installed outside the Git checkout. Dockerfile, Compose,
migrations, the website, .env and SQLite remain manual changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


REPOSITORY = "https://github.com/elenalipovik-ctrl/TGbot-for-meeting-planning.git"
RUNS_API = (
    "https://api.github.com/repos/elenalipovik-ctrl/"
    "TGbot-for-meeting-planning/actions/workflows/ci.yml/runs"
)
APP = Path("/opt/meeting-bot")
CONTROL = Path("/opt/meeting-bot-deploy")
BASELINE = CONTROL / "last_seen_sha"
LOCK = CONTROL / "lock"
IMAGE = "meeting-bot:local"
ROLLBACK_IMAGE = "meeting-bot:rollback"
CONTAINERS = ("meeting-bot-bot-1", "meeting-bot-worker-1")
MIN_FREE_BYTES = 1_000_000_000
MIN_AVAILABLE_KIB = 250_000


class AutoDeployError(RuntimeError):
    """A fixed, safe error code for the technical journal."""


def revision(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise AutoDeployError("invalid_revision")
    return value


def log(event: str, sha: str = "") -> None:
    print(f"auto_deploy event={event} revision={sha[:12] or '-'}", flush=True)


def command(*args: str, timeout: int = 120, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            args, cwd=cwd, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutoDeployError("command_unavailable") from error
    if result.returncode:
        raise AutoDeployError("command_failed")
    return result.stdout.strip()


def remote_head() -> str:
    output = command(
        "git", "-c", "credential.helper=", "ls-remote", REPOSITORY,
        "refs/heads/main", timeout=30,
    )
    parts = output.split()
    if len(parts) != 2 or parts[1] != "refs/heads/main":
        raise AutoDeployError("remote_main_missing")
    return revision(parts[0])


def ci_result(payload: dict, sha: str) -> str:
    runs = [
        run for run in payload.get("workflow_runs", [])
        if run.get("head_sha") == sha and run.get("event") == "push"
    ]
    if not runs:
        return "pending"
    latest = max(
        runs, key=lambda run: (run.get("run_number", 0), run.get("run_attempt", 0))
    )
    if latest.get("status") != "completed":
        return "pending"
    return "success" if latest.get("conclusion") == "success" else "failed"


def ci_status(sha: str) -> str:
    query = urllib.parse.urlencode(
        {"head_sha": sha, "event": "push", "per_page": 10}
    )
    request = urllib.request.Request(
        f"{RUNS_API}?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "meeting-bot-autodeploy",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, ValueError) as error:
        raise AutoDeployError("ci_api_unavailable") from error
    if not isinstance(payload, dict):
        raise AutoDeployError("ci_api_invalid")
    return ci_result(payload, sha)


def unsafe_tracked_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    base = path.name.lower()
    if any(part in {".secrets", "data", "logs"} for part in parts):
        return True
    if base == ".env.example":
        return False
    if base == ".env" or base.startswith((".env.", ".env-")):
        return True
    return base.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm", ".pem", ".key"))


def same_files(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return False
    if left.is_file() and right.is_file():
        return left.read_bytes() == right.read_bytes()
    if not left.is_dir() or not right.is_dir():
        return False
    left_files = {
        item.relative_to(left): item for item in left.rglob("*") if item.is_file()
    }
    right_files = {
        item.relative_to(right): item for item in right.rglob("*") if item.is_file()
    }
    if left_files.keys() != right_files.keys():
        return False
    return all(
        not left_files[name].is_symlink()
        and not right_files[name].is_symlink()
        and left_files[name].read_bytes() == right_files[name].read_bytes()
        for name in left_files
    )


def manual_change_reason(source: Path) -> str | None:
    for name in ("Dockerfile", "compose.yaml", ".dockerignore"):
        if not same_files(source / name, APP / name):
            return "manual_infrastructure_change"
    if not same_files(
        source / "src" / "meeting_bot" / "migrations",
        APP / "src" / "meeting_bot" / "migrations",
    ):
        return "manual_migration_change"
    return None


def validate_source(source: Path, baseline: str, sha: str) -> None:
    actual = revision(command("git", "rev-parse", "HEAD", cwd=source))
    if actual != sha:
        raise AutoDeployError("remote_changed_during_fetch")
    command("git", "merge-base", "--is-ancestor", baseline, sha, cwd=source)
    names = command("git", "ls-files", cwd=source).splitlines()
    if any(unsafe_tracked_path(name) for name in names):
        raise AutoDeployError("unsafe_tracked_path")
    for name in (
        "pyproject.toml", "README.md", "src",
        "scripts/check_server_readiness.py", "scripts/run_server.py",
    ):
        path = source / name
        if not path.exists() or path.is_symlink():
            raise AutoDeployError("runtime_file_missing")
    for path in (source / "src").rglob("*"):
        if path.is_symlink():
            raise AutoDeployError("runtime_symlink")


def check_resources() -> None:
    if shutil.disk_usage("/").free < MIN_FREE_BYTES:
        raise AutoDeployError("insufficient_disk")
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
        available = next(
            int(line.split()[1]) for line in lines
            if line.startswith("MemAvailable:")
        )
    except (OSError, StopIteration, ValueError) as error:
        raise AutoDeployError("memory_unknown") from error
    if available < MIN_AVAILABLE_KIB:
        raise AutoDeployError("insufficient_memory")


def build_context(source: Path, target: Path) -> None:
    target.mkdir(mode=0o700)
    for name in ("Dockerfile", ".dockerignore"):
        shutil.copy2(APP / name, target / name)
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(source / name, target / name)
    shutil.copytree(source / "src", target / "src")
    (target / "scripts").mkdir()
    for name in ("check_server_readiness.py", "run_server.py"):
        shutil.copy2(source / "scripts" / name, target / "scripts" / name)


def preflight(image: str) -> None:
    output = command(
        "docker", "run", "--rm", "--network=none", "--read-only",
        "--user", "10001:10001",
        "--mount", f"type=bind,source={APP / '.env'},target=/app/.env",
        "--mount", f"type=bind,source={APP / 'data'},target=/app/data",
        "--mount", f"type=bind,source={APP / 'logs'},target=/app/logs",
        image, "python", "scripts/check_server_readiness.py", timeout=45,
    )
    if "Server configuration: ready" not in output:
        raise AutoDeployError("preflight_failed")


def compose_up() -> None:
    command(
        "docker", "compose", "-f", str(APP / "compose.yaml"),
        "--project-directory", str(APP), "up", "-d", "--no-build",
        "--force-recreate", "bot", "worker", timeout=180,
    )


def image_id(tag: str) -> str:
    return command(
        "docker", "image", "inspect", "--format", "{{.Id}}", tag, timeout=20
    )


def check_running(expected_image: str) -> None:
    time.sleep(10)
    for name in CONTAINERS:
        state = json.loads(
            command("docker", "inspect", "--format", "{{json .State}}", name)
        )
        used_image = command("docker", "inspect", "--format", "{{.Image}}", name)
        restarts = int(
            command("docker", "inspect", "--format", "{{.RestartCount}}", name)
        )
        if (
            not state.get("Running") or state.get("Restarting")
            or restarts or used_image != expected_image
        ):
            raise AutoDeployError("container_unhealthy")
    connection = sqlite3.connect(
        f"file:{APP / 'data' / 'bot.sqlite'}?mode=ro", uri=True, timeout=5
    )
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise AutoDeployError("sqlite_integrity_failed")
    finally:
        connection.close()


def write_baseline(sha: str) -> None:
    temporary = BASELINE.with_suffix(".new")
    with temporary.open("w", encoding="ascii") as stream:
        stream.write(sha + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, BASELINE)


def deploy(sha: str, baseline: str) -> None:
    check_resources()
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=CONTROL) as temp_name:
        temp = Path(temp_name)
        source = temp / "source"
        command(
            "git", "-c", "credential.helper=", "clone", "--quiet",
            "--single-branch", "--branch", "main", REPOSITORY, str(source),
            timeout=120,
        )
        validate_source(source, baseline, sha)
        reason = manual_change_reason(source)
        if reason:
            raise AutoDeployError(reason)
        context = temp / "build"
        build_context(source, context)
        candidate = f"meeting-bot:candidate-{sha[:12]}"
        command("docker", "build", "--tag", candidate, str(context), timeout=480)
        try:
            preflight(candidate)
            old_id = image_id(IMAGE)
            new_id = image_id(candidate)
            command("docker", "tag", old_id, ROLLBACK_IMAGE)
            command("docker", "tag", candidate, IMAGE)
            try:
                compose_up()
                check_running(new_id)
            except (AutoDeployError, ValueError, sqlite3.Error) as error:
                command("docker", "tag", old_id, IMAGE)
                compose_up()
                check_running(old_id)
                raise AutoDeployError("rolled_back_after_failed_start") from error
            write_baseline(sha)
            log("deployed", sha)
        finally:
            try:
                command("docker", "image", "rm", candidate, timeout=30)
            except AutoDeployError:
                log("candidate_cleanup_failed", sha)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="Inspect next action without deployment"
    )
    arguments = parser.parse_args()
    CONTROL.mkdir(mode=0o700, parents=True, exist_ok=True)
    import fcntl  # Linux-only lock; pure checks above can be tested on Windows.

    with LOCK.open("a+", encoding="ascii") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("already_running")
            return 0
        sha = remote_head()
        if arguments.initialize:
            if BASELINE.exists():
                raise AutoDeployError("already_initialized")
            write_baseline(sha)
            log("baseline_initialized", sha)
            return 0
        if not BASELINE.is_file():
            raise AutoDeployError("baseline_missing")
        baseline = revision(BASELINE.read_text(encoding="ascii"))
        if sha == baseline:
            log("unchanged", sha)
            return 0
        status = ci_status(sha)
        if status != "success":
            log(f"ci_{status}", sha)
            return 0 if status == "pending" else 1
        if arguments.check:
            log("ready_to_deploy", sha)
            return 0
        deploy(sha, baseline)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AutoDeployError as error:
        log(str(error))
        sys.exit(1)
