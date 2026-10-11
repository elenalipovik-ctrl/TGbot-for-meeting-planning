"""Authorized read models shared by chat and the future HTTP adapter."""

from .drafts import require_allowed
from .errors import AccessDenied, RuleError


class Queries:
    def __init__(self, service):
        self.service = service

    def _read(self, actor_id, sql, params=(), *, admin=False):
        connection = self.service._connect()
        try:
            if admin:
                self.service._require_admin(actor_id)
            else:
                require_allowed(connection, actor_id)
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise RuleError("Некорректная страница списка.")

    def own_bookings(self, actor_id, *, limit=20, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT id,starts_at,status,revision FROM bookings WHERE visitor_id=? "
            "AND status!='held' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
            (actor_id, limit, offset))

    def booking(self, actor_id, booking_id, *, administrative=False):
        condition, params = ("id=?", (booking_id,)) if administrative else (
            "id=? AND visitor_id=?", (booking_id, actor_id))
        rows = self._read(actor_id,
            "SELECT id,status,starts_at,ends_at,time_zone,template_name_snapshot,"
            "video_service,contact_name_snapshot,username_snapshot,email_snapshot,"
            "phone_snapshot,purpose,decision_deadline_at,hold_expires_at,revision "
            "FROM bookings WHERE " + condition, params, admin=administrative)
        if not rows:
            raise AccessDenied("Заявка не найдена.")
        return rows[0]

    def templates(self, actor_id, *, administrative=False, limit=100, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT id,name,description,instructions,is_active,revision FROM meeting_templates "
            + ("" if administrative else "WHERE is_active=1 ")
            + "ORDER BY id LIMIT ? OFFSET ?", (limit, offset), admin=administrative)

    def pending_queue(self, actor_id, *, limit=10, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT id,starts_at,status,revision FROM bookings WHERE status IN "
            "('pending','awaiting_link','creating_calendar','confirmed','attention') "
            "ORDER BY submitted_at,id LIMIT ? OFFSET ?", (limit, offset), admin=True)

    def visitors(self, actor_id, *, limit=20, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT telegram_id,username,access_status,revision FROM visitors "
            "ORDER BY telegram_id LIMIT ? OFFSET ?", (limit, offset), admin=True)

    def intervals(self, actor_id, *, limit=100, offset=0):
        self._page(limit, offset)
        return self._read(actor_id,
            "SELECT id,starts_at,ends_at,time_zone,is_published,is_manual_exception,revision "
            "FROM availability_intervals ORDER BY starts_at,id LIMIT ? OFFSET ?",
            (limit, offset), admin=True)

    def stats(self, actor_id, days, now):
        self.service._require_admin(actor_id)
        if days not in (7, 30):
            raise RuleError("Выберите 7 или 30 дней.")
        since = now - days * 86400
        c = self.service._connect()
        try:
            c.execute("BEGIN")
            access = c.execute("SELECT COUNT(*) FROM access_requests WHERE requested_at>=?", (since,)).fetchone()[0]
            rows = c.execute("SELECT status,video_service,used_saved_contacts FROM bookings "
                             "WHERE submitted_at>=?", (since,)).fetchall()
            repeat = c.execute(
                "SELECT COUNT(*) FROM bookings b WHERE b.submitted_at>=? AND b.visitor_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM bookings older WHERE older.visitor_id=b.visitor_id "
                "AND older.submitted_at IS NOT NULL AND (older.submitted_at<b.submitted_at OR "
                "(older.submitted_at=b.submitted_at AND older.id<b.id)))", (since,)).fetchone()[0]
            average = c.execute(
                "SELECT AVG(a.created_at-b.submitted_at) FROM bookings b JOIN audit_actions a "
                "ON a.booking_id=b.id AND a.action IN ('awaiting_link','rejected') "
                "WHERE b.submitted_at>=?", (since,)).fetchone()[0]
            return dict(access=access, total=len(rows),
                confirmed=sum(r[0]=='confirmed' for r in rows),
                rejected=sum(r[0]=='rejected' for r in rows),
                expired=sum(r[0]=='expired' for r in rows),
                cancelled=sum(r[0] in ('cancelled_by_visitor','cancelled_by_admin') for r in rows),
                repeat=repeat, reused=sum(r[2] for r in rows),
                telemost=sum(r[1]=='telemost' for r in rows), zoom=sum(r[1]=='zoom' for r in rows),
                average_minutes=round(average/60) if average is not None else 0)
        finally:
            if c.in_transaction:
                c.execute("ROLLBACK")
            c.close()
