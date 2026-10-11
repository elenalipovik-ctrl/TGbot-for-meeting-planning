import multiprocessing as mp
from pathlib import Path

import pytest

from meeting_bot.database import connect
from meeting_bot.drafts import DraftStore
from meeting_bot.errors import AccessDenied, RuleError, StateConflict
from meeting_bot.service import BookingService
from test_service import ADMIN, VISITOR, OTHER, at, setup_booking, setup_user, submit


@pytest.fixture
def ready(tmp_path):
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN)
    service.initialize()
    now = at(5, 10)
    _, interval, booking = setup_booking(service, now)
    return service, DraftStore(service), now, interval, booking


def patch_worker(path, booking, revision, now, start, results, name):
    service = BookingService(Path(path), administrator_id=ADMIN)
    start.wait(10)
    try:
        DraftStore(service).patch_contacts(VISITOR, VISITOR, booking, now,
                                          expected_revision=revision, contact_name=name)
        results.put("saved")
    except StateConflict:
        results.put("conflict")
    except Exception as error:
        results.put(type(error).__name__)


def test_chat_and_api_share_one_form_without_extending_hold(ready):
    service, chat, now, _, booking = ready
    api = DraftStore(BookingService(service.path, administrator_id=ADMIN))
    old = chat.read(VISITOR, VISITOR, now)
    updated = api.patch_contacts(VISITOR, VISITOR, booking, now+599,
                                 expected_revision=old.revision,
                                 contact_name="Имя", contact_email=" a@example.org ", contact_phone="")
    assert updated.revision > old.revision
    assert updated.values[6:9] == ("Имя", "a@example.org", None)
    assert updated.hold_expires_at == old.hold_expires_at == now+600
    assert chat.read(VISITOR, VISITOR, now+599).values == updated.values
    with pytest.raises(StateConflict):
        chat.save(VISITOR, VISITOR, "email", now+599,
                  expected_revision=old.revision, contact_name="Old")
    expired = chat.read(VISITOR, VISITOR, now+600)
    assert expired.values is None and expired.expired
    assert service.booking_status(booking) == "expired"


def test_delete_and_withdraw_cannot_restore_contacts_or_cancel_hold(ready):
    service, store, now, _, booking = ready
    old = store.read(VISITOR, VISITOR, now)
    store.patch_contacts(VISITOR, VISITOR, booking, now,
                         expected_revision=old.revision, contact_name="Private")
    old = store.read(VISITOR, VISITOR, now)
    service.clear_saved_contacts(VISITOR, VISITOR, now+1)
    current = store.read(VISITOR, VISITOR, now+1)
    assert current.values[6:9] == (None, None, None)
    assert service.booking_status(booking) == "held"
    with pytest.raises(StateConflict):
        store.patch_contacts(VISITOR, VISITOR, booking, now+1,
                             expected_revision=old.revision, contact_name="Private")
    service.withdraw_consent(VISITOR, VISITOR, "policy-v1", now+2)
    with pytest.raises(StateConflict):
        store.patch_contacts(VISITOR, VISITOR, booking, now+2,
                             expected_revision=current.revision, contact_name="Private")
    with pytest.raises(RuleError, match="согласие"):
        submit(service, booking, now+2)


def test_reset_generation_survives_missing_row_and_restart(ready):
    service, store, now, _, _ = ready
    old = store.read(VISITOR, VISITOR, now)
    empty = store.reset(VISITOR, VISITOR, now, expected_revision=old.revision)
    newer = store.reset(VISITOR, VISITOR, now, expected_revision=empty.revision)
    assert newer.values is None and newer.revision > empty.revision > old.revision
    restarted = DraftStore(BookingService(service.path, administrator_id=ADMIN))
    with pytest.raises(StateConflict):
        restarted.save(VISITOR, VISITOR, "name", now, expected_revision=old.revision,
                       contact_name="Must not return")
    service.clear_saved_contacts(VISITOR, VISITOR, now)
    with pytest.raises(StateConflict):
        restarted.save(VISITOR, VISITOR, "name", now, expected_revision=newer.revision)


def test_blocked_and_other_users_cannot_access_forms_or_contacts(ready):
    service, store, now, _, booking = ready
    setup_user(service, OTHER, now)
    with pytest.raises(AccessDenied):
        store.read(OTHER, VISITOR, now)
    with pytest.raises(AccessDenied):
        store.patch_contacts(OTHER, OTHER, booking, now,
                             expected_revision=store.read(OTHER, OTHER, now).revision,
                             contact_name="Foreign")
    service.decide_access(ADMIN, VISITOR, "blocked", now)
    for command in [lambda: store.read(VISITOR, VISITOR, now),
                    lambda: service.saved_contacts(VISITOR, VISITOR),
                    lambda: service.clear_saved_contacts(VISITOR, VISITOR, now),
                    lambda: service.cancel_by_visitor(VISITOR, booking, now)]:
        with pytest.raises(AccessDenied):
            command()


def test_two_processes_editing_same_revision_have_one_winner(ready):
    service, store, now, _, booking = ready
    revision = store.read(VISITOR, VISITOR, now).revision
    ctx = mp.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=patch_worker, args=(str(service.path), booking,
                 revision, now, start, results, name)) for name in ("One", "Two")]
    for process in processes:
        process.start()
    start.set()
    assert sorted(results.get(timeout=20) for _ in processes) == ["conflict", "saved"]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0


def test_booking_revision_rejects_stale_admin_action(ready):
    service, _, now, _, booking = ready
    submit(service, booking, now)
    with connect(service.path) as connection:
        revision = connection.execute("SELECT revision FROM bookings WHERE id=?", (booking,)).fetchone()[0]
    service.admin_decision(ADMIN, booking, True, now, expected_revision=revision)
    with pytest.raises(StateConflict):
        service.admin_decision(ADMIN, booking, False, now, expected_revision=revision)
    assert service.booking_status(booking) == "awaiting_link"
