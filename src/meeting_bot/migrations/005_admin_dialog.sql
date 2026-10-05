CREATE TABLE admin_dialogs (
    admin_id INTEGER PRIMARY KEY,
    step TEXT NOT NULL,
    object_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX admin_dialogs_expiry ON admin_dialogs(expires_at);
