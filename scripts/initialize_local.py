"""Initialize the durable local development database for stage 1."""

from pathlib import Path

from meeting_bot.database import connect, initialize_database
from meeting_bot.technical_log import configure_logging


project_root = Path(__file__).resolve().parents[1]
database_path = project_root / "data" / "bot.sqlite"
logger = configure_logging(project_root / "logs" / "technical.jsonl")
initialize_database(database_path, logger=logger)
connection = connect(database_path, logger=logger)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
finally:
    connection.close()
print(f"SQLite initialized and verified: {database_path}")
