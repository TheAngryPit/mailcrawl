"""SQLite schema v2 tables. user_version 4 records decision origin."""

USER_VERSION = 4

TABLES = (
    "accounts",
    "labels",
    "senders",
    "threads",
    "messages",
    "headers",
    "message_labels",
    "attachments",
    "cursors",
    "import_runs",
    "ledger",
    "decisions",
    "sender_decisions",
    "thread_mutes",
    "msg_fts",
)

HEADER_ALLOW = (
    "list-unsubscribe",
    "list-unsubscribe-post",
    "list-id",
    "precedence",
    "auto-submitted",
    "x-mailer",
    "feedback-id",
    "message-id",
    "in-reply-to",
    "references",
    "reply-to",
    "return-path",
    "authentication-results",
    "received-spf",
)

_SCHEMA_PREFIX = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id INTEGER PRIMARY KEY,
  alias TEXT NOT NULL UNIQUE CHECK (alias IN ('thehive','vcl','personal')),
  email_hash TEXT NOT NULL,
  gog_account_hash TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
  account_id INTEGER NOT NULL REFERENCES accounts,
  gmail_label_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('system','user')),
  axis TEXT,
  in_catalogue INTEGER NOT NULL DEFAULT 0,
  catalogue_key TEXT,
  seen_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, gmail_label_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS labels_name ON labels(account_id, name);

CREATE TABLE IF NOT EXISTS senders (
  sender_id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts,
  address_norm TEXT NOT NULL,
  domain TEXT NOT NULL,
  display_name TEXT,
  first_seen_at INTEGER,
  last_seen_at INTEGER,
  msg_count INTEGER NOT NULL DEFAULT 0,
  has_list_unsubscribe INTEGER NOT NULL DEFAULT 0,
  is_contact INTEGER NOT NULL DEFAULT 0,
  UNIQUE (account_id, address_norm)
);

CREATE TABLE IF NOT EXISTS threads (
  thread_pk INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts,
  gmail_thread_id TEXT NOT NULL,
  first_msg_at INTEGER,
  last_msg_at INTEGER,
  msg_count INTEGER NOT NULL DEFAULT 0,
  last_msg_from_me INTEGER,
  has_human_participant INTEGER,
  subject_norm TEXT,
  UNIQUE (account_id, gmail_thread_id)
);

CREATE TABLE IF NOT EXISTS messages (
  msg_pk INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts,
  gmail_message_id TEXT NOT NULL,
  thread_pk INTEGER NOT NULL REFERENCES threads,
  gmail_history_id TEXT,
  internal_date INTEGER,
  rfc822_message_id TEXT,
  in_reply_to TEXT,
  references_hdr TEXT,
  sender_id INTEGER REFERENCES senders,
  from_hdr TEXT,
  to_hdr TEXT,
  cc_hdr TEXT,
  bcc_hdr TEXT,
  reply_to_hdr TEXT,
  subject TEXT,
  snippet TEXT,
  body_text_raw TEXT,
  body_text TEXT,
  lang TEXT,
  size_bytes INTEGER,
  is_from_me INTEGER NOT NULL DEFAULT 0,
  raw_path TEXT,
  raw_sha256 TEXT NOT NULL,
  raw_json TEXT,
  auth_results TEXT,
  provenance_hash TEXT NOT NULL,
  imported_at INTEGER NOT NULL,
  UNIQUE (account_id, gmail_message_id)
);
CREATE INDEX IF NOT EXISTS msg_thread ON messages(thread_pk, internal_date);
CREATE INDEX IF NOT EXISTS msg_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS msg_date ON messages(internal_date);

CREATE TABLE IF NOT EXISTS headers (
  msg_pk INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (msg_pk, name)
);

CREATE TABLE IF NOT EXISTS message_labels (
  msg_pk INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
  account_id INTEGER NOT NULL,
  gmail_label_id TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  PRIMARY KEY (msg_pk, gmail_label_id),
  FOREIGN KEY (account_id, gmail_label_id) REFERENCES labels(account_id, gmail_label_id)
);

CREATE TABLE IF NOT EXISTS attachments (
  att_pk INTEGER PRIMARY KEY,
  msg_pk INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
  filename TEXT,
  mime_type TEXT,
  size_bytes INTEGER,
  gmail_attachment_id TEXT,
  content_id TEXT,
  is_inline INTEGER NOT NULL DEFAULT 0,
  file_sha256 TEXT,
  stored_path TEXT,
  extract_status TEXT NOT NULL DEFAULT 'pending' CHECK (
    extract_status IN ('pending','ok','empty','error','skipped','unsupported')
  ),
  extract_method TEXT,
  extracted_text TEXT,
  ocr_status TEXT NOT NULL DEFAULT 'not_needed' CHECK (
    ocr_status IN ('not_needed','pending','ok','low_confidence','error')
  ),
  ocr_lang TEXT,
  ocr_conf REAL,
  page_count INTEGER,
  extracted_at INTEGER,
  parser_version TEXT
);
CREATE INDEX IF NOT EXISTS att_hash ON attachments(file_sha256);

CREATE TABLE IF NOT EXISTS cursors (
  account_id INTEGER NOT NULL,
  stream TEXT NOT NULL,
  value TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, stream)
);

CREATE TABLE IF NOT EXISTS import_runs (
  run_id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  source_manifest_sha256 TEXT,
  started_at INTEGER,
  finished_at INTEGER,
  n_seen INTEGER,
  n_new INTEGER,
  n_dup INTEGER,
  n_error INTEGER,
  mailcrawl_version TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  account_id INTEGER,
  ref TEXT,
  detail_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
  SELECT RAISE(ABORT, 'ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
  SELECT RAISE(ABORT, 'ledger is append-only');
END;

"""

V2_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY,
  thread_pk INTEGER NOT NULL REFERENCES threads,
  next_state TEXT NOT NULL CHECK (next_state IN (
    'reply','action','waiting','read_later','reference','noise','needs_review','resolved','screener'
  )),
  priority INTEGER CHECK (priority BETWEEN 0 AND 3),
  owner TEXT CHECK (owner IN ('me','other','none')),
  review_date INTEGER,
  due_date INTEGER,
  lane TEXT CHECK (lane IN ('imbox','feed','paper_trail','screened_out')),
  pinned_aside INTEGER NOT NULL DEFAULT 0,
  muted INTEGER NOT NULL DEFAULT 0,
  confidence REAL,
  source TEXT NOT NULL CHECK (source IN ('rule','embedding-cluster','llm','human','legacy-label')),
  origin TEXT NOT NULL DEFAULT 'rule' CHECK (origin IN ('rule','knn','model','human')),
  source_detail TEXT NOT NULL DEFAULT '',
  rule_id TEXT,
  rules_version TEXT NOT NULL,
  evidence TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  superseded_by INTEGER REFERENCES decisions(decision_id),
  CHECK (next_state <> 'waiting' OR review_date IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS decisions_current ON decisions(thread_pk) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS sender_decisions (
  sender_id INTEGER PRIMARY KEY REFERENCES senders,
  status TEXT NOT NULL CHECK (status IN ('legacy','pending','approved','denied')),
  route TEXT CHECK (route IN ('imbox','feed','paper_trail','read')),
  scope TEXT NOT NULL DEFAULT 'sender' CHECK (scope IN ('sender','domain')),
  bundle INTEGER NOT NULL DEFAULT 0,
  priority INTEGER,
  client_sla_days INTEGER,
  decided_by TEXT,
  decided_at INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS thread_mutes (
  thread_pk INTEGER PRIMARY KEY REFERENCES threads,
  muted_at INTEGER NOT NULL,
  until INTEGER,
  reason TEXT
);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
  subject,
  body_text,
  from_hdr,
  attachment_text,
  content='',
  tokenize='unicode61 remove_diacritics 2'
);
"""

SCHEMA_SQL = _SCHEMA_PREFIX + V2_TABLES_SQL + FTS_SQL
