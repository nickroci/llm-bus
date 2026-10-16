CREATE TABLE message_reports (
  message_id TEXT PRIMARY KEY REFERENCES messages(id),
  revision INTEGER NOT NULL CHECK(revision > 0),
  status TEXT NOT NULL,
  reason TEXT,
  expected_revision INTEGER NOT NULL
);
