import pytest

from meeting_bot.commands import Commands
from meeting_bot.database import connect
from meeting_bot.errors import AccessDenied, RuleError, StateConflict, StorageUnavailable
from meeting_bot.queries import Queries
from test_service import ADMIN, VISITOR, OTHER, setup_user, submit
from test_shared_drafts import ready


class Calendar:
    def __init__(self, path):
        self.path, self.calls = path, 0

    def freebusy(self, start, end):
        # A second writer can enter: no SQLite transaction spans the network call.
        with connect(self.path, busy_timeout_ms=10) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
        self.calls += 1
        return []


def test_submit_uses_canonical_contacts_and_rechecks_revision(ready):
    service, store, now, _, booking = ready
    calendar = Calendar(service.path)
    commands = Commands(service, calendar, policy_version="policy-v1")
    old = store.read(VISITOR, VISITOR, now)
    current = store.patch_contacts(VISITOR, VISITOR, booking, now,
        expected_revision=old.revision, contact_name="Canonical", contact_email="valid@example.org", purpose="Goal")
    with pytest.raises(StateConflict):
        commands.submit(VISITOR, booking, username="trusted", now=now, revision=old.revision)
    assert calendar.calls == 0
    assert commands.submit(VISITOR, booking, username="trusted", now=now, revision=current.revision) == booking
    with connect(service.path) as c:
        assert c.execute("SELECT contact_name_snapshot,username_snapshot,email_snapshot FROM bookings WHERE id=?", (booking,)).fetchone() == ("Canonical", "trusted", "valid@example.org")
    assert calendar.calls == 1


def test_no_foreign_or_blocked_data_or_google_call(ready):
    service, store, now, _, booking = ready
    setup_user(service, OTHER, now)
    calendar = Calendar(service.path)
    commands = Commands(service, calendar, policy_version="policy-v1")
    queries = Queries(service)
    with pytest.raises(AccessDenied):
        queries.booking(OTHER, booking)
    with pytest.raises(AccessDenied):
        queries.pending_queue(OTHER)
    with pytest.raises(AccessDenied):
        commands.confirm(OTHER, booking, "https://telemost.yandex.ru/j/123", now, revision=0)
    service.decide_access(ADMIN, VISITOR, "blocked", now)
    for call in [lambda: queries.own_bookings(VISITOR),
                 lambda: queries.templates(VISITOR),
                 lambda: commands.submit(VISITOR, booking, username="trusted", now=now, revision=0)]:
        with pytest.raises(AccessDenied):
            call()
    assert calendar.calls == 0


def test_invalid_url_revision_and_text_cannot_bypass_shared_validation(ready):
    service, store, now, _, booking = ready
    revision = store.read(VISITOR, VISITOR, now).revision
    with pytest.raises(StateConflict):
        store.patch_contacts(VISITOR, VISITOR, booking, now, expected_revision=None, contact_name="Name")
    with pytest.raises(RuleError):
        store.patch_contacts(VISITOR, VISITOR, booking, now, expected_revision=revision, purpose="x"*501)
    submit(service, booking, now)
    service.admin_decision(ADMIN, booking, True, now)
    query = Queries(service)
    rev = query.booking(ADMIN, booking, administrative=True)[14]
    calendar = Calendar(service.path)
    commands = Commands(service, calendar, policy_version="policy-v1")
    with pytest.raises(RuleError):
        commands.confirm(ADMIN, booking, "https://telemost.yandex.ru.evil.test/j/123", now, revision=rev)
    with pytest.raises(StateConflict):
        commands.cancel(VISITOR, booking, now, revision=None)
    assert service.booking_status(booking) == "awaiting_link" and calendar.calls == 0


def test_busy_draft_reports_safe_error_and_does_not_write(ready, monkeypatch):
    service, store, now, _, _ = ready
    with connect(service.path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(service, "_connect", lambda: connect(service.path, busy_timeout_ms=10))
        with pytest.raises(StorageUnavailable, match="Попробуйте"):
            store.read(VISITOR, VISITOR, now)
        blocker.execute("ROLLBACK")


def test_template_revision_rejects_lost_update(ready):
    service, _, now, _, _ = ready
    revision = Queries(service).templates(ADMIN, administrative=True)[0][5]
    service.update_template(ADMIN, 1, name="New", description="", instructions="", is_active=True, now=now, expected_revision=revision)
    with pytest.raises(StateConflict):
        service.update_template(ADMIN, 1, name="Old", description="", instructions="", is_active=True, now=now, expected_revision=revision)


def test_profile_change_invalidates_old_form_and_old_consent(ready):
    service, store, now, _, booking = ready
    old = store.read(VISITOR, VISITOR, now)
    service.correct_saved_contacts(VISITOR, VISITOR, name="Current", email="new@example.org",
                                   phone=None, now=now)
    with pytest.raises(StateConflict):
        service.correct_saved_contacts(VISITOR, VISITOR, name="Old", email="old@example.org",
            phone=None, now=now, expected_draft_revision=old.revision)
    with pytest.raises(StateConflict):
        service.record_consent(VISITOR, "policy-v1", now, expected_draft_revision=old.revision)
    assert service.saved_contacts(VISITOR, VISITOR).email == "new@example.org"
