-- Canonical editable form: visitor_dialogs. drafts keeps only held-slot metadata.
-- Preserve the hold already displayed in chat; otherwise keep the newest hold.
CREATE TEMP TABLE retained_holds AS
SELECT visitor_id, COALESCE(
    (SELECT b.id FROM visitor_dialogs d JOIN bookings b ON b.id = d.booking_id
     WHERE d.visitor_id = candidates.visitor_id AND b.status = 'held'
       AND b.visitor_id = candidates.visitor_id),
    (SELECT b.id FROM bookings b WHERE b.visitor_id = candidates.visitor_id
       AND b.status = 'held' ORDER BY b.created_at DESC, b.id DESC LIMIT 1)
) AS booking_id
FROM bookings candidates WHERE status = 'held' AND visitor_id IS NOT NULL
GROUP BY visitor_id;

UPDATE bookings SET status = 'expired', completed_at = updated_at
WHERE status = 'held' AND visitor_id IS NOT NULL
  AND id NOT IN (SELECT booking_id FROM retained_holds);
UPDATE jobs SET status = 'done', claimed_until = NULL
WHERE kind = 'hold_expire' AND booking_id IN
    (SELECT id FROM bookings WHERE status != 'held');

INSERT INTO visitor_dialogs
    (visitor_id, step, template_id, duration_minutes, video_service, booking_id,
     contact_name, contact_email, contact_phone, purpose, expires_at, updated_at)
SELECT h.visitor_id, 'contacts', b.template_id, (b.ends_at - b.starts_at) / 60,
       b.video_service, b.id, d.contact_name, d.contact_email, d.contact_phone,
       d.purpose, b.hold_expires_at, b.updated_at
FROM retained_holds h JOIN bookings b ON b.id = h.booking_id
LEFT JOIN drafts d ON d.id = (SELECT MAX(x.id) FROM drafts x WHERE x.booking_id = b.id)
WHERE NOT EXISTS (SELECT 1 FROM visitor_dialogs v WHERE v.visitor_id = h.visitor_id);

-- A stale pre-hold dialog must point at the retained reservation, without
-- retaining contacts entered for a different reservation.
UPDATE visitor_dialogs SET
    step='contacts', booking_id=(SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id),
    template_id=(SELECT template_id FROM bookings WHERE id=(SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id)),
    duration_minutes=(SELECT (ends_at-starts_at)/60 FROM bookings WHERE id=(SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id)),
    video_service=(SELECT video_service FROM bookings WHERE id=(SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id)),
    expires_at=(SELECT hold_expires_at FROM bookings WHERE id=(SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id)),
    contact_name=NULL, contact_email=NULL, contact_phone=NULL, purpose=NULL, used_saved_contacts=0
WHERE visitor_id IN (SELECT visitor_id FROM retained_holds)
  AND booking_id IS NOT (SELECT booking_id FROM retained_holds h WHERE h.visitor_id=visitor_dialogs.visitor_id);

-- Only fill canonical fields from the metadata for the SAME selected hold.
UPDATE visitor_dialogs SET
    contact_name = COALESCE(contact_name,
        (SELECT contact_name FROM drafts WHERE booking_id = visitor_dialogs.booking_id ORDER BY id DESC LIMIT 1)),
    contact_email = COALESCE(contact_email,
        (SELECT contact_email FROM drafts WHERE booking_id = visitor_dialogs.booking_id ORDER BY id DESC LIMIT 1)),
    contact_phone = COALESCE(contact_phone,
        (SELECT contact_phone FROM drafts WHERE booking_id = visitor_dialogs.booking_id ORDER BY id DESC LIMIT 1)),
    purpose = COALESCE(purpose,
        (SELECT purpose FROM drafts WHERE booking_id = visitor_dialogs.booking_id ORDER BY id DESC LIMIT 1))
WHERE booking_id IN (SELECT booking_id FROM retained_holds);

DELETE FROM drafts WHERE booking_id IS NULL
    OR booking_id NOT IN (SELECT booking_id FROM retained_holds);
DELETE FROM drafts WHERE id NOT IN (SELECT MAX(id) FROM drafts GROUP BY visitor_id);
UPDATE drafts SET contact_name = NULL, contact_email = NULL, contact_phone = NULL, purpose = NULL;
DROP TABLE retained_holds;

CREATE UNIQUE INDEX one_hold_per_visitor ON bookings(visitor_id)
WHERE status = 'held' AND visitor_id IS NOT NULL;
CREATE UNIQUE INDEX one_hold_metadata_per_visitor ON drafts(visitor_id);

ALTER TABLE visitor_dialogs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE draft_generations (
    visitor_id INTEGER PRIMARY KEY REFERENCES visitors(telegram_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT INTO draft_generations(visitor_id, revision)
SELECT telegram_id, CASE WHEN EXISTS
    (SELECT 1 FROM visitor_dialogs WHERE visitor_id = visitors.telegram_id) THEN 1 ELSE 0 END
FROM visitors;
UPDATE visitor_dialogs SET revision = 1;

CREATE TRIGGER initialize_draft_generation AFTER INSERT ON visitors
BEGIN
    INSERT INTO draft_generations(visitor_id) VALUES (NEW.telegram_id);
END;
CREATE TRIGGER draft_insert_revision AFTER INSERT ON visitor_dialogs
BEGIN
    UPDATE draft_generations SET revision = revision + 1 WHERE visitor_id = NEW.visitor_id;
    UPDATE visitor_dialogs SET revision =
        (SELECT revision FROM draft_generations WHERE visitor_id = NEW.visitor_id)
    WHERE visitor_id = NEW.visitor_id;
END;
CREATE TRIGGER draft_update_revision AFTER UPDATE ON visitor_dialogs
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE draft_generations SET revision = revision + 1 WHERE visitor_id = NEW.visitor_id;
    UPDATE visitor_dialogs SET revision =
        (SELECT revision FROM draft_generations WHERE visitor_id = NEW.visitor_id)
    WHERE visitor_id = NEW.visitor_id;
END;
CREATE TRIGGER draft_delete_revision AFTER DELETE ON visitor_dialogs
BEGIN
    UPDATE draft_generations SET revision = revision + 1 WHERE visitor_id = OLD.visitor_id;
END;

ALTER TABLE bookings ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE visitors ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meeting_templates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE availability_intervals ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

CREATE TRIGGER booking_revision AFTER UPDATE ON bookings
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE bookings SET revision = OLD.revision + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER visitor_revision AFTER UPDATE ON visitors
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE visitors SET revision = OLD.revision + 1 WHERE telegram_id = NEW.telegram_id;
END;
CREATE TRIGGER template_revision AFTER UPDATE ON meeting_templates
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE meeting_templates SET revision = OLD.revision + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER interval_revision AFTER UPDATE ON availability_intervals
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE availability_intervals SET revision = OLD.revision + 1 WHERE id = NEW.id;
END;
