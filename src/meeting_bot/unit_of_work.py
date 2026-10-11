"""Bind short domain transactions to one durable command transaction.

Each service call gets its own savepoint handle. ContextVar isolates request threads.
No external calls may run inside atomic(); prepare external availability beforehand.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import uuid

from .database import connect

_bound = ContextVar("meeting_bot_transaction", default=None)
_events = ContextVar("meeting_bot_transaction_events", default=None)

def defer_log(logger, event, fields):
    pending = _events.get()
    if pending is None:
        return False
    pending.append((logger, event, fields))
    return True



class BorrowedConnection:
    def __init__(self, connection):
        self.connection, self.savepoint = connection, None

    @property
    def in_transaction(self):
        return self.savepoint is not None

    def execute(self, sql, parameters=()):
        command = sql.strip().upper()
        if command in ("BEGIN", "BEGIN IMMEDIATE"):
            if self.savepoint:
                raise RuntimeError("Nested transaction on same handle")
            self.savepoint = "domain_" + uuid.uuid4().hex
            return self.connection.execute("SAVEPOINT " + self.savepoint)
        if command == "COMMIT":
            if self.savepoint is None:
                raise RuntimeError("Missing domain transaction")
            cursor = self.connection.execute("RELEASE " + self.savepoint)
            self.savepoint = None
            return cursor
        if command == "ROLLBACK":
            if self.savepoint is None:
                raise RuntimeError("Missing domain transaction")
            self.connection.execute("ROLLBACK TO " + self.savepoint)
            cursor = self.connection.execute("RELEASE " + self.savepoint)
            self.savepoint = None
            return cursor
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection.executemany(sql, parameters)

    def close(self):
        if self.savepoint:
            self.execute("ROLLBACK")


def borrowed(path):
    current = _bound.get()
    if current and current[0] == Path(path).resolve():
        return BorrowedConnection(current[1])
    return None


@contextmanager
def atomic(path, *, logger=None):
    if _bound.get() is not None:
        raise RuntimeError("Nested command transaction")
    connection = connect(path, logger=logger)
    token = None
    events_token = None
    pending = []
    committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        token = _bound.set((Path(path).resolve(), connection))
        events_token = _events.set(pending)
        yield connection
        connection.execute("COMMIT")
        committed = True
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if token is not None:
            _bound.reset(token)
        connection.close()
        if events_token is not None:
            _events.reset(events_token)
        if committed:
            from .technical_log import log_event
            for target, event, fields in pending:
                log_event(target, event, **fields)
