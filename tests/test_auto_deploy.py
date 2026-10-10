import os
import stat
from pathlib import Path

import pytest

from scripts import auto_deploy


SHA = "a" * 40
OTHER = "b" * 40


def test_ci_accepts_only_latest_successful_push_for_exact_commit():
    payload = {
        "workflow_runs": [
            {"head_sha": OTHER, "event": "push", "status": "completed",
             "conclusion": "success", "run_number": 50, "run_attempt": 1},
            {"head_sha": SHA, "event": "push", "status": "completed",
             "conclusion": "success", "run_number": 2, "run_attempt": 1},
            {"head_sha": SHA, "event": "push", "status": "completed",
             "conclusion": "failure", "run_number": 2, "run_attempt": 2},
        ]
    }
    assert auto_deploy.ci_result(payload, SHA) == "failed"
    payload["workflow_runs"][-1]["status"] = "in_progress"
    assert auto_deploy.ci_result(payload, SHA) == "pending"
    payload["workflow_runs"][-1].update(status="completed", conclusion="success")
    assert auto_deploy.ci_result(payload, SHA) == "success"


def test_secret_paths_are_rejected_but_empty_template_is_allowed():
    assert not auto_deploy.unsafe_tracked_path(".env.example")
    for name in (".env", ".env.local", "config/.env", "data/bot.sqlite",
                 "logs/technical.jsonl", ".secrets/google.json", "key.pem"):
        assert auto_deploy.unsafe_tracked_path(name)


def test_infrastructure_and_migrations_require_manual_deploy(tmp_path, monkeypatch):
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    for root in (live, candidate):
        migrations = root / "src" / "meeting_bot" / "migrations"
        migrations.mkdir(parents=True)
        (migrations / "001.sql").write_text("CREATE TABLE x(y);", encoding="utf-8")
        for name in ("Dockerfile", "compose.yaml", ".dockerignore"):
            (root / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(auto_deploy, "APP", live)
    assert auto_deploy.manual_change_reason(candidate) is None
    (candidate / "compose.yaml").write_text("changed", encoding="utf-8")
    assert auto_deploy.manual_change_reason(candidate) == "manual_infrastructure_change"
    (candidate / "compose.yaml").write_text("compose.yaml", encoding="utf-8")
    (candidate / "src" / "meeting_bot" / "migrations" / "002.sql").write_text(
        "ALTER TABLE x ADD z;", encoding="utf-8"
    )
    assert auto_deploy.manual_change_reason(candidate) == "manual_migration_change"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are tested in CI")
def test_build_context_is_readable_by_unprivileged_image_user(tmp_path, monkeypatch):
    live = tmp_path / "live"
    source = tmp_path / "source"
    (source / "src" / "meeting_bot").mkdir(parents=True)
    (source / "scripts").mkdir()
    live.mkdir()
    for name in ("Dockerfile", ".dockerignore"):
        (live / name).write_text(name, encoding="utf-8")
    for name in ("pyproject.toml", "README.md"):
        (source / name).write_text(name, encoding="utf-8")
    for name in ("check_server_readiness.py", "run_server.py"):
        (source / "scripts" / name).write_text(name, encoding="utf-8")
    package = source / "src" / "meeting_bot"
    (package / "__init__.py").write_text("", encoding="utf-8")
    package.chmod(0o700)
    (package / "__init__.py").chmod(0o600)
    monkeypatch.setattr(auto_deploy, "APP", live)

    target = tmp_path / "build"
    auto_deploy.build_context(source, target)
    assert stat.S_IMODE((target / "src" / "meeting_bot").stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "src" / "meeting_bot" / "__init__.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((target / "scripts").stat().st_mode) == 0o755


def test_failed_start_restores_old_image(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(auto_deploy, "CONTROL", control)
    monkeypatch.setattr(auto_deploy, "check_resources", lambda: None)
    monkeypatch.setattr(auto_deploy, "validate_source", lambda *args: None)
    monkeypatch.setattr(auto_deploy, "manual_change_reason", lambda *args: None)
    monkeypatch.setattr(auto_deploy, "build_context", lambda *args: None)
    monkeypatch.setattr(auto_deploy, "preflight", lambda *args: None)
    monkeypatch.setattr(auto_deploy, "image_id", lambda tag: "old" if tag == auto_deploy.IMAGE else "new")
    monkeypatch.setattr(auto_deploy, "compose_up", lambda: None)
    checked = []

    def check_running(image):
        checked.append(image)
        if image == "new":
            raise auto_deploy.AutoDeployError("container_unhealthy")

    monkeypatch.setattr(auto_deploy, "check_running", check_running)
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        return ""

    monkeypatch.setattr(auto_deploy, "command", command)
    with pytest.raises(auto_deploy.AutoDeployError, match="rolled_back_after_failed_start"):
        auto_deploy.deploy(SHA, OTHER)
    assert checked == ["new", "old"]
    assert ("docker", "tag", "old", auto_deploy.IMAGE) in commands
