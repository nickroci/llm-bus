ALTER TABLE messages ADD COLUMN retention_at TEXT;
ALTER TABLE messages ADD COLUMN report_expires_at TEXT;
-- A malformed legacy age aborts migration before any retention comparison or purge.
CREATE TABLE retention_age_validation (valid INTEGER NOT NULL CHECK(valid=1));
INSERT INTO retention_age_validation SELECT CASE
  WHEN strftime('%Y-%m-%dT%H:%M:%fZ',sent_at) IS NOT NULL
    AND substr(sent_at,11,1)='T'
    AND CAST(substr(sent_at,1,4) AS INTEGER) BETWEEN 1 AND 9999
    AND date(substr(sent_at,1,10),'+0 days')=substr(sent_at,1,10)
    AND (substr(sent_at,-1)='Z' OR (substr(sent_at,-6,1) IN ('+','-') AND substr(sent_at,-3,1)=':'))
  THEN 1 ELSE 0 END FROM messages;
UPDATE messages SET retention_at=strftime('%Y-%m-%dT%H:%M:%fZ',sent_at);
DROP TABLE retention_age_validation;
-- NULL report_expires_at marks legacy rows: their advertised 30-day deadline is immutable.
CREATE INDEX messages_retention ON messages(retention_at,sequence);
CREATE TABLE purged_message_ids (id TEXT PRIMARY KEY);
