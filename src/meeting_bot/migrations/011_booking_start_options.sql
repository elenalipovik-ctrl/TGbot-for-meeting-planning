-- Optional shared start choices. Empty preserves the existing 15-minute grid.
CREATE TABLE booking_start_options (minute INTEGER PRIMARY KEY CHECK(minute BETWEEN 0 AND 1439 AND minute % 15 = 0));
INSERT INTO setting_revisions(name) VALUES ('booking_start_options');
CREATE TRIGGER start_option_insert AFTER INSERT ON booking_start_options
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='booking_start_options'; END;
CREATE TRIGGER start_option_delete AFTER DELETE ON booking_start_options
BEGIN UPDATE setting_revisions SET revision=revision+1 WHERE name='booking_start_options'; END;
