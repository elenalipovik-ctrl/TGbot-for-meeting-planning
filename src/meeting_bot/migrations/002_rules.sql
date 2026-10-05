CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

INSERT INTO app_settings (key, value, updated_at)
VALUES ('time_zone', 'Europe/Moscow', CAST(strftime('%s', 'now') AS INTEGER));

CREATE TABLE review_windows (
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    starts_minute INTEGER NOT NULL CHECK (starts_minute BETWEEN 0 AND 1439),
    ends_minute INTEGER NOT NULL CHECK (ends_minute BETWEEN 1 AND 1440),
    PRIMARY KEY (weekday, starts_minute),
    CHECK (ends_minute > starts_minute)
);

INSERT INTO review_windows (weekday, starts_minute, ends_minute)
VALUES (0, 600, 1140), (1, 600, 1140), (2, 600, 1140),
       (3, 600, 1140), (4, 600, 1140);

CREATE TABLE bookings_v2 (
    id INTEGER PRIMARY KEY,
    visitor_id INTEGER REFERENCES visitors(telegram_id) ON DELETE SET NULL,
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
    cancel_requested_at INTEGER,
    CHECK (ends_at > starts_at)
);

INSERT INTO bookings_v2 (
    id, visitor_id, template_id, availability_id, starts_at, ends_at,
    time_zone, status, hold_expires_at, submitted_at, decision_deadline_at,
    contact_name_snapshot, username_snapshot, email_snapshot, phone_snapshot,
    template_name_snapshot, template_description_snapshot,
    template_instructions_snapshot, purpose, video_service, created_at,
    updated_at, completed_at
)
SELECT id, visitor_id, template_id, availability_id, starts_at, ends_at,
       time_zone, status, hold_expires_at, submitted_at, decision_deadline_at,
       contact_name_snapshot, username_snapshot, email_snapshot, phone_snapshot,
       template_name_snapshot, template_description_snapshot,
       template_instructions_snapshot, purpose, video_service, created_at,
       updated_at, completed_at
FROM bookings;

DROP TABLE bookings;
ALTER TABLE bookings_v2 RENAME TO bookings;

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

ALTER TABLE drafts ADD COLUMN booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX drafts_booking ON drafts(booking_id);
