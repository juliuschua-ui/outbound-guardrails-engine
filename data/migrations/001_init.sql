-- 001_init.sql — tables. Transcribed from PLAN.md §3.2.
-- Applied migrations are IMMUTABLE: schema changes are always a new numbered file.

CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per company. prospect_key is THE identity + variant-assignment key.
CREATE TABLE prospects (
  id               INTEGER PRIMARY KEY,
  prospect_key     TEXT NOT NULL UNIQUE,   -- normalized domain ('acmehardscapeaz.com')
                                           -- or 'name:<slug>:<market-slug>' when no website
  company_name     TEXT NOT NULL,
  company_casual   TEXT,                   -- casualized form; REQUIRED before compose
  domain           TEXT,                   -- NULL when no website
  website_url      TEXT,
  market           TEXT NOT NULL,          -- free-form 'City ST', e.g. 'Boise ID'
  us_state         TEXT NOT NULL,          -- 2-letter, drives timezone
  timezone         TEXT NOT NULL,          -- IANA, e.g. 'America/Boise'
  owner_first_name TEXT,
  owner_last_name  TEXT,
  icp_fit_note     TEXT,
  source           TEXT NOT NULL,          -- 'lead-scout'|'gmaps-leads'|'scrape-leads'|'manual'
  source_detail    TEXT,
  state            TEXT NOT NULL DEFAULT 'NEW'
                   CHECK (state IN ('NEW','QUALIFIED','EXCLUDED','SUPPRESSED')),
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_prospects_domain ON prospects(domain);
CREATE INDEX idx_prospects_market ON prospects(market);

-- A reachable identity of a human at a prospect, per channel.
CREATE TABLE contacts (
  id              INTEGER PRIMARY KEY,
  prospect_id     INTEGER NOT NULL REFERENCES prospects(id),
  person_key      TEXT,                    -- normalized email OR linkedin slug; detects the
                                           -- same human across companies
  channel         TEXT NOT NULL CHECK (channel IN ('email','linkedin','instagram','sms')),
  address         TEXT NOT NULL,           -- email addr / profile URL
  address_source  TEXT NOT NULL CHECK (address_source IN
                    ('website','reply','linkedin','directory','apollo','manual','format_guess')),
  verify_verdict  TEXT CHECK (verify_verdict IN ('PASS','RISK','FAIL')),
  verify_reason   TEXT,
  verified_at     TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (channel, address)
);
CREATE INDEX idx_contacts_person ON contacts(person_key);
CREATE INDEX idx_contacts_prospect ON contacts(prospect_id);

CREATE TABLE suppression (
  id          INTEGER PRIMARY KEY,
  channel     TEXT NOT NULL CHECK (channel IN ('email','linkedin','instagram','sms','all')),
  address     TEXT NOT NULL,               -- lowercase email / profile URL / '@domain' for scope=domain
  scope       TEXT NOT NULL DEFAULT 'address' CHECK (scope IN ('address','domain','person')),
  reason      TEXT NOT NULL CHECK (reason IN
               ('hard_no','unsubscribe','bounce','dead_mailbox','legal','manual')),
  source      TEXT,                        -- 'file_sync'|'reply_scan'|'dmctl'|...
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (channel, address)
);

CREATE TABLE experiments (
  id          TEXT PRIMARY KEY,            -- 'EXP-2026-08-skeptic'
  name        TEXT NOT NULL,
  hypothesis  TEXT,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Variants are IMMUTABLE once any message references them.
-- An edit = new row (family stays, version increments), old row retired.
CREATE TABLE variants (
  id             TEXT PRIMARY KEY,         -- 'V1.1' (family V1, version 1)
  family         TEXT NOT NULL,            -- 'V1'..'V5'
  version        INTEGER NOT NULL DEFAULT 1,
  experiment_id  TEXT NOT NULL REFERENCES experiments(id),
  name           TEXT NOT NULL,
  angle          TEXT NOT NULL,            -- one-line skeptic angle
  spec           TEXT NOT NULL,            -- JSON: structure, required_tokens, opener guidance,
                                           -- example soft questions (see §5.2)
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  min_n_gate     INTEGER NOT NULL DEFAULT 30,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (experiment_id, family, version)
);

CREATE TABLE batches (
  id               TEXT PRIMARY KEY,       -- 'B-20260803-boise-1'
  experiment_id    TEXT NOT NULL REFERENCES experiments(id),
  market           TEXT NOT NULL,
  channel          TEXT NOT NULL CHECK (channel IN ('email','linkedin')),
  personalization  TEXT NOT NULL DEFAULT 'unique'
                   CHECK (personalization IN ('template','unique')),
  batch_seq        INTEGER NOT NULL,       -- monotonically increasing per experiment; used
                                           -- for cross-batch round-robin offset (§4.3)
  state            TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN
                    ('OPEN','COMPOSED','REVIEWED','STAGED','CLOSED','ABORTED')),
  notes            TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE assignments (
  id                INTEGER PRIMARY KEY,
  experiment_id     TEXT NOT NULL REFERENCES experiments(id),
  batch_id          TEXT NOT NULL REFERENCES batches(id),
  prospect_id       INTEGER NOT NULL REFERENCES prospects(id),
  variant_id        TEXT NOT NULL REFERENCES variants(id),
  algo              TEXT NOT NULL DEFAULT 'sorted-hash-rr-v1',
  assign_hash       TEXT NOT NULL,         -- sha256(experiment_id + '|' + prospect_key) hex
  variant_set_hash  TEXT NOT NULL,         -- sha256 of ordered active variant ids at assign time
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (experiment_id, prospect_id)      -- one variant per prospect per experiment, forever
);
CREATE INDEX idx_assign_variant ON assignments(experiment_id, variant_id);

CREATE TABLE messages (
  id             INTEGER PRIMARY KEY,
  batch_id       TEXT NOT NULL REFERENCES batches(id),
  prospect_id    INTEGER NOT NULL REFERENCES prospects(id),
  contact_id     INTEGER NOT NULL REFERENCES contacts(id),
  assignment_id  INTEGER NOT NULL REFERENCES assignments(id),
  variant_id     TEXT NOT NULL REFERENCES variants(id),   -- denormalized for the sheet query
  sequence_step  INTEGER NOT NULL DEFAULT 1 CHECK (sequence_step IN (1,2)),
  channel        TEXT NOT NULL CHECK (channel IN ('email','linkedin')),
  subject        TEXT,                     -- email only; NULL for linkedin
  body           TEXT NOT NULL,
  state          TEXT NOT NULL DEFAULT 'COMPOSED' CHECK (state IN
                  ('COMPOSED','LINT_FAILED','READY','APPROVED','STAGED','SENT',
                   'BOUNCED','REPLIED','CLOSED','REJECTED','CANCELLED')),
  lint_report    TEXT,                     -- JSON LintReport
  approved_at    TEXT,
  approved_via   TEXT,                     -- 'review_file:<path>'|'interactive_tty'
  gmail_draft_id   TEXT,
  gmail_message_id TEXT,
  gmail_thread_id  TEXT,
  staged_at      TEXT,
  sent_at        TEXT,
  sent_evidence  TEXT,                     -- 'gmail_sent_scan:<msgid>'|'manual_confirm:<note>'
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_state ON messages(state);
CREATE INDEX idx_messages_prospect ON messages(prospect_id);
CREATE INDEX idx_messages_batch ON messages(batch_id);

-- Append-only audit of every state change. NEVER updated or deleted.
CREATE TABLE state_log (
  id          INTEGER PRIMARY KEY,
  entity      TEXT NOT NULL,               -- 'message'|'prospect'|'batch'
  entity_id   TEXT NOT NULL,
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  actor       TEXT NOT NULL,               -- 'operator'|'engine'|'agent:<name>'
  reason      TEXT,
  evidence    TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE replies (
  id               INTEGER PRIMARY KEY,
  prospect_id      INTEGER NOT NULL REFERENCES prospects(id),
  message_id       INTEGER REFERENCES messages(id),   -- NULL if thread-match failed
  gmail_message_id TEXT UNIQUE,            -- dedup key for the scanner
  received_at      TEXT NOT NULL,
  classification   TEXT NOT NULL CHECK (classification IN
                    ('hot','question','objection','no','unsure',
                     'bounce','auto_reply','vacation')),
  is_human         INTEGER NOT NULL DEFAULT 0,  -- 1 unless bounce/auto_reply/vacation
  snippet          TEXT,                   -- <=300 chars
  classified_by    TEXT NOT NULL,          -- 'heuristic'|'triage_file:<name>'|'operator'
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_replies_prospect ON replies(prospect_id);

CREATE TABLE outcomes (
  id                      INTEGER PRIMARY KEY,
  prospect_id             INTEGER NOT NULL UNIQUE REFERENCES prospects(id),
  outcome                 TEXT NOT NULL CHECK (outcome IN
                           ('in_progress','demo_booked','call_booked',
                            'replied_no','unsubscribed','dead')),
  demo_booked_at          TEXT,
  attribution_method      TEXT CHECK (attribution_method IN
                           ('utm','domain_match','name_phone_match','manual_confirm')),
  attribution_confidence  TEXT CHECK (attribution_confidence IN ('exact','high','manual')),
  ghl_contact_id          TEXT,
  evidence                TEXT,
  created_at              TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cap accounting: one row per staged artifact, keyed to the Phoenix calendar day.
CREATE TABLE stage_events (
  id               INTEGER PRIMARY KEY,
  message_id       INTEGER NOT NULL UNIQUE REFERENCES messages(id),
  channel          TEXT NOT NULL,
  personalization  TEXT NOT NULL,
  phx_date         TEXT NOT NULL,          -- 'YYYY-MM-DD' computed at UTC-7 fixed
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_stage_phx ON stage_events(phx_date, channel);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  command     TEXT NOT NULL,
  args        TEXT,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status      TEXT CHECK (status IN ('ok','failed','partial')),
  detail      TEXT
);
