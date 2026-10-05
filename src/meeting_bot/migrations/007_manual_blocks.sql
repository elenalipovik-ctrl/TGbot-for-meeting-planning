CREATE TABLE manual_blocks (
    id INTEGER PRIMARY KEY,
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE INDEX manual_blocks_by_time ON manual_blocks(starts_at, ends_at);
