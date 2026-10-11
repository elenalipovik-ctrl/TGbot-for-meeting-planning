"""Transport-independent entry points; actor identity comes from verified transport.

HTTP adapters must never dispatch browser callback strings or expose service internals.
The policy version and calendar are application configuration, not request fields.
"""

from .calendar_flow import confirm_with_google, submit_with_google
from .drafts import DraftStore
from .errors import RuleError, StateConflict
from .queries import Queries
from .validation import MAX_ADMIN_TEXT, valid_video_url


class Commands:
    def __init__(self, service, calendar, *, policy_version):
        self.service, self.calendar = service, calendar
        self.policy_version = policy_version
        self.drafts = DraftStore(service)
        self.queries = Queries(service)

    @staticmethod
    def _revision(value):
        if type(value) is not int or value < 0:
            raise StateConflict("Укажите текущую версию данных.")

    def create_hold(self, actor_id, template_id, interval_id, starts_at,
                    duration_minutes, video_service, now, *, revision, google_busy=None):
        self._revision(revision)
        snapshot = self.drafts.read(actor_id, actor_id, now)
        if snapshot.revision != revision:
            raise StateConflict("Запись уже изменена. Обновите экран.")
        busy = google_busy if google_busy is not None else self.calendar.freebusy(starts_at-1800, starts_at+duration_minutes*60+1800)
        return self.service.create_hold(actor_id, template_id, interval_id, starts_at,
            duration_minutes, video_service, now, google_busy=busy,
            expected_draft_revision=revision)

    def submit(self, actor_id, booking_id, *, username, now, revision, google_busy=None):
        self._revision(revision)
        snapshot = self.drafts.read(actor_id, actor_id, now)
        if snapshot.revision != revision:
            raise StateConflict("Запись уже изменена. Обновите экран.")
        form = snapshot.values
        if not form or form[5] != booking_id:
            raise RuleError("Удержание времени истекло или не найдено.")
        self.queries.booking(actor_id, booking_id)
        return submit_with_google(self.service, self.calendar, actor_id, booking_id,
            name=form[6] or "", username=username, email=form[7] or "", phone=form[8],
            purpose=form[9] or "", policy_version=self.policy_version, now=now,
            used_saved_contacts=bool(form[11]), expected_draft_revision=revision, google_busy=google_busy)

    def confirm(self, actor_id, booking_id, video_url, now, *, revision):
        self._revision(revision)
        self.service._require_admin(actor_id)
        booking = self.queries.booking(actor_id, booking_id, administrative=True)
        if type(revision) is not int or revision != booking[14]:
            raise StateConflict("Заявка уже изменена. Обновите экран.")
        if len(video_url) > MAX_ADMIN_TEXT or not valid_video_url(video_url, booking[6]):
            raise RuleError("Пришлите корректную HTTPS-ссылку выбранного видеосервиса.")
        return confirm_with_google(self.service, self.calendar, booking_id,
                                   video_url.strip(), now, expected_revision=revision)

    def consent(self, actor_id, now, *, revision):
        self._revision(revision)
        self.drafts.read(actor_id, actor_id, now)
        return self.service.record_consent(actor_id, self.policy_version, now,
                                           expected_draft_revision=revision)

    def clear_contacts(self, actor_id, now, *, revision):
        self._revision(revision)
        return self.service.clear_saved_contacts(actor_id, actor_id, now,
                                                  expected_draft_revision=revision)

    def withdraw_consent(self, actor_id, now, *, revision):
        self._revision(revision)
        return self.service.withdraw_consent(actor_id, actor_id, self.policy_version,
                                              now, expected_draft_revision=revision)

    def decide(self, actor_id, booking_id, approve, now, *, revision):
        self._revision(revision)
        return self.service.admin_decision(actor_id, booking_id, approve, now,
                                           expected_revision=revision)

    def cancel(self, actor_id, booking_id, now, *, revision, administrative=False):
        self._revision(revision)
        if administrative:
            return self.service.cancel_by_admin(actor_id, booking_id, now,
                                                expected_revision=revision)
        return self.service.cancel_by_visitor(actor_id, booking_id, now,
                                              expected_revision=revision)
