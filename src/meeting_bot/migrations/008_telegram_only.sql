-- The administrator chose Telegram as the only service-notification channel.
-- Earlier email notices were never delivered because no sender was configured.
DELETE FROM notifications WHERE channel = 'email';

CREATE TRIGGER notifications_telegram_only_insert
BEFORE INSERT ON notifications
WHEN NEW.channel <> 'telegram'
BEGIN
    SELECT RAISE(ABORT, 'unsupported_notification_channel');
END;

CREATE TRIGGER notifications_telegram_only_update
BEFORE UPDATE OF channel ON notifications
WHEN NEW.channel <> 'telegram'
BEGIN
    SELECT RAISE(ABORT, 'unsupported_notification_channel');
END;
