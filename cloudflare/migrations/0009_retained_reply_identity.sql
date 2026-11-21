-- Preserve accepted envelope identity independently of the nullable parent FK.
ALTER TABLE messages ADD COLUMN accepted_reply_to TEXT;
UPDATE messages SET accepted_reply_to=reply_to;
