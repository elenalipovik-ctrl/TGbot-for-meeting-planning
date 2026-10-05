ALTER TABLE bookings ADD COLUMN used_saved_contacts INTEGER NOT NULL DEFAULT 0
    CHECK (used_saved_contacts IN (0, 1));

ALTER TABLE visitor_dialogs ADD COLUMN used_saved_contacts INTEGER NOT NULL DEFAULT 0
    CHECK (used_saved_contacts IN (0, 1));
