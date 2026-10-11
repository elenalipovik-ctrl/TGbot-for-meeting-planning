-- No bearer/initData/request bodies or contact copies are persisted here.
CREATE TABLE api_sessions (
    token_hash TEXT PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    username TEXT,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE INDEX api_sessions_expiry ON api_sessions(expires_at);
CREATE TABLE api_auth_exchanges (
    fingerprint TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE TABLE api_rate_limits (
    scope_hash TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(scope_hash,bucket)
);
CREATE TABLE api_operations (
    id TEXT PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    route TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('done','accepted')),
    result_ref TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    lease_token TEXT,
    lease_until INTEGER,
    UNIQUE(actor_id,route,key_hash)
);
CREATE INDEX api_operations_expiry ON api_operations(expires_at);
CREATE TABLE integration_checks (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    error_code TEXT
);
ALTER TABLE manual_blocks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER manual_block_revision AFTER UPDATE ON manual_blocks
WHEN NEW.revision=OLD.revision
BEGIN
    UPDATE manual_blocks SET revision=OLD.revision+1 WHERE id=NEW.id;
END;
CREATE TABLE setting_revisions (name TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
INSERT INTO setting_revisions(name) VALUES ('review_schedule');
CREATE TRIGGER review_window_insert AFTER INSERT ON review_windows
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='review_schedule'; END;
CREATE TRIGGER review_window_update AFTER UPDATE ON review_windows
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='review_schedule'; END;
CREATE TRIGGER review_window_delete AFTER DELETE ON review_windows
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='review_schedule'; END;
CREATE TRIGGER review_timezone_update AFTER UPDATE ON app_settings
WHEN NEW.key='time_zone' AND NEW.value IS NOT OLD.value
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='review_schedule'; END;

CREATE INDEX api_auth_expiry ON api_auth_exchanges(expires_at);
CREATE INDEX api_rate_expiry ON api_rate_limits(expires_at);
