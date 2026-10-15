CREATE TABLE machines (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  revoked_at TEXT
);
CREATE TABLE repo_grants (
  machine_id TEXT NOT NULL REFERENCES machines(id),
  repo_id TEXT NOT NULL,
  PRIMARY KEY (machine_id, repo_id)
);
CREATE TABLE sessions (
  address TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL REFERENCES machines(id),
  repo_id TEXT NOT NULL,
  native_address TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  ended INTEGER NOT NULL DEFAULT 0,
  UNIQUE(machine_id, repo_id, native_address)
);
CREATE TABLE messages (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  sender TEXT NOT NULL REFERENCES sessions(address),
  recipient TEXT NOT NULL REFERENCES sessions(address),
  repo_id TEXT NOT NULL,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  ready INTEGER NOT NULL DEFAULT 0,
  received_at TEXT,
  acknowledged_at TEXT
);
CREATE INDEX mailbox ON messages(recipient, ready, received_at, sequence);
CREATE INDEX dispatch ON messages(ready, sequence);
