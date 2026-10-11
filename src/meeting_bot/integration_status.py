"""Common verified Google status. Never return busy intervals or credential details."""

from .google_calendar import CalendarAuthExpired, CalendarError, CalendarPermissionDenied


def google_status(service, actor_id, now):
    service._require_admin(actor_id)
    c = service._connect()
    try:
        row = c.execute("SELECT status,checked_at,error_code FROM integration_checks WHERE name='google'").fetchone()
    finally:
        c.close()
    if row is None:
        return dict(status="not_checked", checked_at=None, error_code=None, stale=True)
    return dict(status=row[0], checked_at=row[1], error_code=row[2], stale=now-row[1]>300)


def check_google(service, calendar, actor_id, now):
    service._require_admin(actor_id)
    try:
        calendar.freebusy(now, now+60)
        status, code = "working", None
    except CalendarAuthExpired:
        status, code = "reconnect_required", "auth_expired"
    except CalendarPermissionDenied:
        status, code = "permission_denied", "permission_denied"
    except CalendarError:
        status, code = "unavailable", "unavailable"
    c = service._connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO integration_checks(name,status,checked_at,error_code) VALUES ('google',?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,error_code=excluded.error_code",
                  (status, now, code))
        if code in ("auth_expired", "permission_denied"):
            c.execute("INSERT OR IGNORE INTO notifications(channel,kind,status,dedup_key) "
                      "VALUES ('telegram','google_connection_attention','queued',?)",
                      (f"google_connection_attention:{code}:{now//3600}",))
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return google_status(service, actor_id, now)
