CREATE TABLE visitor_dialogs (
    visitor_id INTEGER PRIMARY KEY REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    template_id INTEGER REFERENCES meeting_templates(id) ON DELETE SET NULL,
    duration_minutes INTEGER,
    video_service TEXT CHECK (video_service IN ('telemost', 'zoom')),
    selected_day TEXT,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    purpose TEXT,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX visitor_dialogs_by_expiry ON visitor_dialogs(expires_at);
