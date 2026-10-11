"""Common safe read models for Mini App; no HTTP, secrets or raw Google data."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import AccessDenied, RuleError
from .queries import Queries
from .rules import can_cancel_confirmed


class MiniAppQueries(Queries):
    def access_status(self, actor_id):
        c = self.service._connect()
        try:
            row = c.execute("SELECT access_status FROM visitors WHERE telegram_id=?", (actor_id,)).fetchone()
            return row[0] if row else "new"
        finally:
            c.close()

    def profile(self, actor_id):
        rows = self._read(actor_id,
            "SELECT display_name,email,phone,username,revision FROM visitors WHERE telegram_id=?", (actor_id,))
        if not rows:
            raise AccessDenied("Посетитель не найден.")
        name, email, phone, username, revision = rows[0]
        return dict(name=name, email=email, phone=phone, username=username, revision=revision)

    def booking_details(self, actor_id, booking_id, now, *, administrative=False):
        condition = "b.id=?" if administrative else "b.id=? AND b.visitor_id=?"
        params = (booking_id,) if administrative else (booking_id, actor_id)
        rows = self._read(actor_id,
            "SELECT b.id,b.status,b.starts_at,b.ends_at,b.time_zone,b.video_service,"
            "b.template_name_snapshot,b.template_description_snapshot,b.template_instructions_snapshot,"
            "b.contact_name_snapshot,b.username_snapshot,b.email_snapshot,b.phone_snapshot,b.purpose,"
            "b.hold_expires_at,b.decision_deadline_at,b.revision,b.cancel_requested_at,"
            "m.video_url,m.sync_status FROM bookings b LEFT JOIN meetings m ON m.booking_id=b.id WHERE " + condition,
            params, admin=administrative)
        if not rows:
            raise AccessDenied("Заявка не найдена.")
        r = rows[0]
        actions = []
        if administrative:
            if r[1] == "pending" and r[15] is not None and now < r[15]:
                actions += ["approve", "reject"]
            if r[1] == "awaiting_link" and r[15] is not None and now < r[15]:
                actions += ["video_link"]
            if r[1] in ("creating_calendar", "attention") and r[17] is None:
                actions += ["retry_calendar"]
            if r[1] in ("pending", "awaiting_link", "confirmed"):
                actions += ["cancel"]
        elif r[1] in ("pending", "awaiting_link") or (r[1] == "confirmed" and can_cancel_confirmed(now, r[2])):
            actions += ["cancel"]
        if r[1] == "held" and not administrative and r[14] is not None and now < r[14]:
            actions += ["edit_draft", "release"]
        return dict(id=r[0], status=r[1], starts_at=r[2], ends_at=r[3], time_zone=r[4],
            duration_minutes=(r[3]-r[2])//60, video_service=r[5],
            template=dict(name=r[6], description=r[7], instructions=r[8]),
            contacts=dict(name=r[9], username=r[10], email=r[11], phone=r[12]),
            purpose=r[13], hold_expires_at=r[14], decision_deadline_at=r[15], revision=r[16],
            cancel_until=r[2]-3600 if r[1] == "confirmed" else None,
            cancellation_pending=r[17] is not None,
            video_url=r[18] if administrative or (r[1] == "confirmed" and r[17] is None) else None,
            sync_status=r[19], allowed_actions=actions)

    def bookings(self, actor_id, *, administrative=False, status=None, from_time=None, to_time=None, group=None, now=None, limit=20, offset=0):
        self._page(limit, offset)
        valid = {"pending", "awaiting_link", "creating_calendar", "confirmed", "attention",
                 "cancelled_by_visitor", "cancelled_by_admin", "rejected", "expired"}
        if status is not None and status not in valid:
            raise RuleError("Некорректный статус.")
        clauses, params = ["status!='held'"], []
        if not administrative:
            clauses.append("visitor_id=?")
            params.append(actor_id)
        if from_time is not None and to_time is not None and from_time >= to_time:
            raise RuleError("Некорректный диапазон дат.")
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if from_time is not None:
            clauses.append("starts_at>=?")
            params.append(from_time)
        if to_time is not None:
            clauses.append("starts_at<?")
            params.append(to_time)
        order="COALESCE(decision_deadline_at,9223372036854775807),id" if administrative and status in ("pending","awaiting_link") else "id DESC"
        if group is not None:
            if group not in ("review","upcoming","history") or now is None:
                raise RuleError("Некорректная группа заявок.")
            if group=="review":
                clauses.append("status IN ('pending','awaiting_link','creating_calendar','attention')")
                order="COALESCE(decision_deadline_at,9223372036854775807),id"
            elif group=="upcoming":
                clauses.append("status='confirmed' AND ends_at>?")
                params.append(now)
                order="starts_at,id"
            else:
                clauses.append("(status IN ('rejected','expired','cancelled_by_visitor','cancelled_by_admin') OR (status='confirmed' AND ends_at<=?))")
                params.append(now)
                order="COALESCE(completed_at,ends_at) DESC,id DESC"
        return self._read(actor_id,
            "SELECT id,starts_at,status,revision,ends_at,time_zone,template_name_snapshot,video_service,decision_deadline_at FROM bookings WHERE " + " AND ".join(clauses)
            + " ORDER BY "+order+" LIMIT ? OFFSET ?", (*params, limit, offset), admin=administrative)

    def access_requests(self, actor_id, *, limit=20, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT ar.id,ar.visitor_id,v.username,ar.requested_at,v.revision FROM access_requests ar "
            "JOIN visitors v ON v.telegram_id=ar.visitor_id WHERE ar.status='pending' "
            "ORDER BY ar.id LIMIT ? OFFSET ?", (limit, offset), admin=True)

    def filtered_visitors(self, actor_id, *, status=None, limit=20, offset=0):
        self._page(limit, offset)
        if status is not None and status not in ("pending", "allowed", "blocked", "rejected"):
            raise RuleError("Некорректный статус.")
        return self._read(actor_id,
            "SELECT telegram_id,username,access_status,revision FROM visitors "
            + ("WHERE access_status=? " if status else "")
            + "ORDER BY telegram_id LIMIT ? OFFSET ?", ((status,) if status else ())+(limit, offset), admin=True)

    def manual_blocks(self, actor_id, *, limit=20, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT id,starts_at,ends_at,revision FROM manual_blocks ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset), admin=True)

    def impact(self, actor_id, *, kind, interval_id=None, local_date=None, starts_at=None, ends_at=None):
        self.service._require_admin(actor_id)
        if kind == "interval":
            clause, params = "availability_id=?", (interval_id,)
        else:
            if kind == "day":
                zone = ZoneInfo(self.service.review_schedule().time_zone)
                starts_at = int(datetime.combine(local_date, time.min, zone).timestamp())
                ends_at = int(datetime.combine(local_date+timedelta(days=1), time.min, zone).timestamp())
            if starts_at is None or ends_at is None or starts_at >= ends_at:
                raise RuleError("Некорректный диапазон.")
            clause, params = "starts_at<? AND ends_at>?", (ends_at, starts_at)
        rows = self._read(actor_id,
            "SELECT id,status FROM bookings WHERE status IN "
            "('held','pending','awaiting_link','creating_calendar','confirmed','attention') AND " + clause,
            params, admin=True)
        return dict(booking_ids=[r[0] for r in rows], count=len(rows), preview=True)

    def schedule_details(self, actor_id):
        self.service._require_admin(actor_id)
        c = self.service._connect()
        try:
            c.execute("BEGIN")
            schedule = self.service._schedule(c)
            revision = c.execute("SELECT revision FROM setting_revisions WHERE name='review_schedule'").fetchone()[0]
            return dict(time_zone=schedule.time_zone, revision=revision,
                        windows=[dict(weekday=w.weekday, starts_minute=w.starts_minute, ends_minute=w.ends_minute) for w in schedule.windows])
        finally:
            if c.in_transaction:
                c.execute("ROLLBACK")
            c.close()

    def alerts(self, actor_id):
        rows = self._read(actor_id,
            "SELECT (SELECT COUNT(*) FROM bookings WHERE status='attention'),"
            "(SELECT COUNT(*) FROM notifications WHERE status='failed'),"
            "(SELECT COUNT(*) FROM jobs WHERE status='claimed')", admin=True)
        ids=self._read(actor_id,"SELECT DISTINCT booking_id FROM notifications WHERE status='failed' AND booking_id IS NOT NULL ORDER BY booking_id LIMIT 100",admin=True)
        awaiting=self._read(actor_id,"SELECT COUNT(*) FROM bookings WHERE status='awaiting_link'",admin=True)[0][0]
        return dict(calendar_attention=rows[0][0], notification_failures=rows[0][1], claimed_jobs=rows[0][2],awaiting_link=awaiting,notification_booking_ids=[r[0] for r in ids])
