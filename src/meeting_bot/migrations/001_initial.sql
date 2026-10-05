CREATE TABLE visitors (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    email TEXT,
    phone TEXT,
    access_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (access_status IN ('pending', 'allowed', 'rejected', 'blocked')),
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);

CREATE TABLE consents (
    id INTEGER PRIMARY KEY,
    visitor_id INTEGER NOT NULL REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    accepted_at INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('accepted', 'withdrawn'))
);

CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY,
    visitor_id INTEGER NOT NULL REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'allowed', 'rejected', 'blocked')),
    requested_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER
);

CREATE TABLE meeting_templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE availability_intervals (
    id INTEGER PRIMARY KEY,
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL,
    time_zone TEXT NOT NULL,
    is_published INTEGER NOT NULL DEFAULT 1 CHECK (is_published IN (0, 1)),
    is_manual_exception INTEGER NOT NULL DEFAULT 0 CHECK (is_manual_exception IN (0, 1)),
    created_at INTEGER NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE INDEX availability_by_time ON availability_intervals(starts_at, ends_at);

CREATE TABLE drafts (
    id INTEGER PRIMARY KEY,
    visitor_id INTEGER NOT NULL REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    template_id INTEGER REFERENCES meeting_templates(id) ON DELETE SET NULL,
    availability_id INTEGER REFERENCES availability_intervals(id) ON DELETE SET NULL,
    starts_at INTEGER,
    ends_at INTEGER,
    time_zone TEXT,
    video_service TEXT CHECK (video_service IN ('telemost', 'zoom')),
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    purpose TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at)
);

CREATE INDEX drafts_by_expiry ON drafts(expires_at);

CREATE TABLE bookings (
    id INTEGER PRIMARY KEY,
    visitor_id INTEGER NOT NULL REFERENCES visitors(telegram_id) ON DELETE RESTRICT,
    template_id INTEGER REFERENCES meeting_templates(id) ON DELETE SET NULL,
    availability_id INTEGER REFERENCES availability_intervals(id) ON DELETE SET NULL,
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL,
    time_zone TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed',
        'rejected', 'expired', 'cancelled_by_visitor', 'cancelled_by_admin',
        'attention'
    )),
    hold_expires_at INTEGER,
    submitted_at INTEGER,
    decision_deadline_at INTEGER,
    contact_name_snapshot TEXT,
    username_snapshot TEXT,
    email_snapshot TEXT,
    phone_snapshot TEXT,
    template_name_snapshot TEXT,
    template_description_snapshot TEXT,
    template_instructions_snapshot TEXT,
    purpose TEXT,
    video_service TEXT CHECK (video_service IN ('telemost', 'zoom')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    CHECK (ends_at > starts_at)
);

CREATE INDEX bookings_by_time ON bookings(starts_at, ends_at, status);
CREATE INDEX bookings_by_visitor ON bookings(visitor_id, created_at);
CREATE INDEX bookings_by_completion ON bookings(completed_at);

CREATE TRIGGER bookings_no_overlap_insert BEFORE INSERT ON bookings
WHEN NEW.status IN ('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')
AND EXISTS (
    SELECT 1 FROM bookings AS old
    WHERE old.status IN ('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')
      AND NEW.starts_at < old.ends_at + 1800
      AND old.starts_at < NEW.ends_at + 1800
)
BEGIN
    SELECT RAISE(ABORT, 'slot_conflict');
END;

CREATE TRIGGER bookings_no_overlap_update BEFORE UPDATE OF starts_at, ends_at, status ON bookings
WHEN NEW.status IN ('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')
AND EXISTS (
    SELECT 1 FROM bookings AS old
    WHERE old.id != NEW.id
      AND old.status IN ('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')
      AND NEW.starts_at < old.ends_at + 1800
      AND old.starts_at < NEW.ends_at + 1800
)
BEGIN
    SELECT RAISE(ABORT, 'slot_conflict');
END;

CREATE TABLE meetings (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE RESTRICT,
    google_event_id TEXT UNIQUE,
    video_url TEXT,
    sync_status TEXT NOT NULL CHECK (sync_status IN ('pending', 'synced', 'attention', 'cancelled')),
    last_synced_at INTEGER
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE CASCADE,
    visitor_id INTEGER REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at INTEGER,
    sent_at INTEGER,
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    run_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'claimed', 'done', 'failed')),
    claimed_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE INDEX jobs_due ON jobs(status, run_at);

CREATE TABLE audit_actions (
    id INTEGER PRIMARY KEY,
    actor_telegram_id INTEGER,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
