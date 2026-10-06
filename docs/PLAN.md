# Outbound DM Engine — Implementation Plan (PLAN.md)

**Status:** ARCHITECT-COMPLETE, ready for parallel build.
**Project root:** `the repo root` (Windows view: `C:\Users\user\work\...` does not apply — this tree lives WSL-side only; mirror path if ever needed: `\\wsl$\...`).
**Runtime:** Python 3.14 stdlib + sqlite3 3.46.1. No web framework, no ORM, no queue broker, no new services, no paid dependencies.
**Builders:** 5 parallel owners (A–E), disjoint file sets, all cross-module contracts defined in this document. Builders have NO further access to the operator; every ambiguity resolved here.

---

## 1. Problem restatement + success criteria

### 1.1 Problem (paraphrased from a third-party outbound-strategy assessment)

- **Pipeline gap:** too few prospects from the target customer profile were reaching the product demo, and there was no repeatable, low-cost way to pull them in. The pipeline was not thin so much as not running.
- **Highest lever:** an AI-assisted outbound system that finds where the target customers spend time online, drafts personalized cold DMs or emails in their own language, and queues the outreach without hand-writing every message. No ad budget.
- **Articulation gap:** the buyer is skeptical and has often already tried a cheaper fix and been burned, so generic missed-call messaging does not land. The copy has to name that experience.
- **Quick win:** five short cold openers (under three sentences each, pain first, one soft question), run across ten real prospects.
- **Measure this:** a sheet with the columns `message variant | who sent to | did they reply (y/n) | did they book the demo`. After about twenty attempts there is real signal, and the sheet becomes the foundation of the whole system.
- **Path forward:** ship twenty outreach attempts, then measure reply rate and demo bookings.

The DM Engine is the machine that makes those 20 attempts (and the next 500) cheap, safe, and *measured* — generalizing the existing the earlier email pipeline email pipeline into a channel-agnostic, variant-instrumented system with the measurement sheet as its spine.

### 1.2 Success criteria (verifiable, numeric)

| # | Criterion | Verification | Report line it discharges |
|---|---|---|---|
| SC-1 | ≥20 Message-1 attempts reach `SENT` state, spread across ≥10 distinct prospects and exactly 5 active variants (V1–V5) | `sqlite3 data/dm.db "SELECT count(*) FROM messages WHERE sequence_step=1 AND state IN ('SENT','REPLIED','BOUNCED','CLOSED')"` ≥ 20; `SELECT count(DISTINCT variant_id) ...` = 5 | Quick Win + Path Forward ("Ship 20 outreach attempts") |
| SC-2 | The measurement sheet — exactly the report's 4 columns — falls out of **one query** (`sheet` view) covering 100% of attempts, exportable to CSV | `dmctl sheet --csv /tmp/sheet.csv` row count == attempt count; columns = variant, sent_to, replied, booked | "MEASURE THIS… That sheet becomes the foundation" |
| SC-3 | Per-variant reply rate and booking rate computed automatically, with honest sample-size gating (no winner below n=30/variant) | `dmctl stats` output shows counts + Wilson CI + gate banner | "real signal on what message is landing"; "measure reply rate and demo bookings" |
| SC-4 | Marginal cost per attempt = $0 (no ads, no paid data, no paid sending tools) | Dependency audit: stdlib + already-installed google-api-python-client + dnspython only; no new API keys with billing | "No ad budget needed", "Zero Runway" |
| SC-5 | All 5 seed variants encode the skeptic-first angle ("tried a cheaper solution and got burned") with pain-first structure and soft closing question, each ≤3 sentences in DM form | `data/variants/EXP-2026-08-skeptic.json` review + lint enforces structure | Articulation Gap + Quick Win spec |
| SC-6 | Zero gate violations across the run: 0 auto-sends, 0 Message-1s with links/product name, 0 messages to FAIL-verified or suppressed addresses, 0 cap breaches | `tests/adversarial/test_gates.py` green + `SELECT count(*) FROM state_log WHERE reason LIKE 'VIOLATION%'` = 0 | Standing rules (system must be safe to run repeatedly) |
| SC-7 | Demo bookings attributable to a variant for every booking that occurs (method + confidence recorded) | `outcomes.attribution_method` non-null for every `demo_booked` row | "did they book the demo" column; Pipeline Gap ("ICP going through your demo") |
| SC-8 | Engine surfaces per-variant verbatim reply text at low n (the *actual* signal at n=20) | `dmctl stats --replies` prints replies grouped by variant | "After 20 outreach attempts you will have real signal" |

---

## 2. System architecture

### 2.1 Module map

```
dm-engine/
├── docs/PLAN.md                  (this file)
├── data/
│   ├── dm.db                     SQLite database (gitignored)
│   ├── migrations/               numbered .sql files (committed)
│   ├── variants/                 versioned variant definition JSON (committed)
│   ├── review/                   human review files (gitignored)
│   ├── outbox/
│   │   ├── linkedin/             human-send cards for LinkedIn DMs
│   │   └── dry-run/              --dry-run staging output
│   └── locks/                    flock files
├── src/dm_engine/
│   ├── config.py                 paths, env, test-mode guard          [A]
│   ├── db.py                     connect + migrations runner          [A]
│   ├── locks.py                  flock-based single-writer lock       [A]
│   ├── prospects.py              intake, normalize, dedup             [B]
│   ├── suppression.py            suppression list, file sync          [B]
│   ├── variants.py               variant registry + deterministic
│   │                             assignment                           [B]
│   ├── stats.py                  per-variant stats, Wilson CI, gates  [B]
│   ├── compose.py                compose-job emit + draft ingest      [C]
│   ├── lint.py                   mechanical message gates             [C]
│   ├── state.py                  state machine + transition log       [D]
│   ├── approval.py               review files + approval ingest       [D]
│   ├── gmail_compat.py           shim importing the gmail-helper
│   │                             skill scripts (verify, drafts)       [D]
│   ├── channels/
│   │   ├── base.py               ChannelAdapter protocol              [D]
│   │   ├── email_gmail.py        Gmail-draft staging + reconcile      [D]
│   │   └── linkedin_manual.py    human-send queue cards               [D]
│   ├── replies.py                Gmail reply scan + classification    [E]
│   ├── attribution.py            demo-booking join logic              [E]
│   ├── report.py                 sheet view/CSV, DASHBOARD metrics       [E]
│   └── cli.py                    `dmctl` entrypoint                   [E]
└── tests/                        pytest; fakes; fixtures; adversarial
```

### 2.2 Data flow (text diagram)

```
 lead-scout / gmaps-leads / scrape-leads agents        (existing skills, unchanged)
        │  prospects.json (contract §12.7)
        ▼
 [prospects.py] import → normalize domain → dedup → suppression check
        │  state: NEW ──(human/agent qualify)──► QUALIFIED
        ▼
 [variants.py] deterministic assignment (experiment, batch)   ← data/variants/*.json
        │  assignments row (reproducible: sorted-hash round-robin)
        ▼
 [compose.py] emit compose-job JSON ──► outreach-specialist agent (+ /de-ai-ify,
        │                               /voice-extractor, /casualize-names)
        │  ◄── drafts JSON (per-prospect subject+body)
        ▼
 [lint.py] mechanical gates: tokens resolved, no links, no product name,
        │  banned phrases, length, lowercase subject, unique subjects
        │  fail → LINT_FAILED (never proceeds)   pass → READY
        ▼
 [approval.py] review file written → THE OPERATOR EDITS IT → approve ingest
        │  READY ──► APPROVED   (no other path; DB trigger enforced)
        ▼
 [channels/*] stage APPROVED only:
        │   email    → Gmail draft in operatorchua.aj (verify gate re-checked)
        │   linkedin → human-send card in data/outbox/linkedin/ + DASHBOARD approvals
        │  state: APPROVED ──► STAGED
        ▼
 ═══ THE ENGINE STOPS HERE. IT CONTAINS ZERO SEND CODE. ═══
        │  actual send: the operator in Gmail / DASHBOARD EXEC send-queue (its own
        │  APPROVE+ARM double gate, VPS host guard) / manual LinkedIn send
        ▼
 [email_gmail.reconcile / dmctl mark-sent] detect real sends from Gmail Sent
        │  STAGED ──► SENT (evidence recorded)   bounces → BOUNCED + suppression
        ▼
 [replies.py] inbox scan → human-reply vs bounce/auto-responder → classify
        │  (dashboard-reply-triage output ingestable)   SENT ──► REPLIED
        ▼
 [Message 2] reply-gated (trigger: step-2 APPROVED requires a human reply row)
        │  same approve→stage path; demo link + utm_content=<prospect_key>
        ▼
 [attribution.py] GHL export (from disk) + UTM + domain/name match → outcomes
        ▼
 [report.py] sheet view (variant|sent_to|replied|booked) → CSV
             stats.py per-variant table → dmctl stats
             DASHBOARD metrics JSON → ~/work/dashboard/data/metrics/pull-*.json
```

### 2.3 What the engine reuses (verified on disk, exact paths)

| Existing asset | How the engine uses it |
|---|---|
| `~/.config/skills/gmail-helper/scripts/verify_emails.py` | imported via `gmail_compat.py`: `verify_batch(emails, smtp=False) -> list[{email, verdict, reason, sendable}]`, constants `PASS, RISK, FAIL` |
| `~/.config/skills/gmail-helper/scripts/create_drafts.py` | imported: `create_draft(service, to, subject, body) -> draft_id`, `get_service(account=None) -> (service, email)`, `load_suppression() -> set`, `already_sent_to(service, email) -> bool`, `has_inbound_reply(service, email) -> bool` (fail-closed), `DEMO_LINK = "brightdesk.brightflowcure.com/demo-ai-voice"`, `DEFAULT_ACCOUNT = "operator_personal"` |
| `~/.config/skills/gmail-helper/scripts/_sender_host_guard.py` | NOT needed by the engine (it never sends) — but documented so builders don't add send code "for convenience"; any future send script must call `require_sender_host()` |
| `~/.config/skills/gmail-helper/suppression_list.txt` | two-way sync (import always before staging; export appends bounced emails) |
| `~/.config/skills/gmail-helper/gmail_token.json` + `gmail_accounts.json` | Gmail auth. Read-only scans (reconcile, replies) load token WITHOUT a scopes list — the `gmail_send_queue.py` precedent: "scope enforcement at load breaks refresh with invalid_scope" |
| DASHBOARD EXEC send-queue (`the external send-queue`) | the preferred real-send path. Integration surface = Gmail drafts only: EXEC's `gmail_send_queue.py list-drafts` sees engine-staged drafts; the operator APPROVEs + ARMs there; EXEC enforces WINDOW_CAP=10, DAY_CAP=20, Phoenix UTC-7, expiry, auto-disarm. The engine never writes to `dashboard.db`. |
| DASHBOARD metrics contract | write `~/work/dashboard/data/metrics/pull-<YYYY-MM-DD-HHmm>.json` as `{"generatedAt": ISO, "metrics": {plain numbers only}}`; omit unavailable keys; never delete old files (dashboard-metrics-pull SKILL.md contract) |
| DASHBOARD approvals lane | write `~/work/dashboard/data/approvals/dmengine-<YYYY-MM-DD-HHmm>.json` in the exec-card shape: `{"items":[{"id","title","summary","link"}]}` (matches `exec-ticket849.json` on disk) |
| earlier email-outreach skill + lead-sourcing and copywriting agents | unchanged upstream producers; engine consumes their output as JSON files (contracts §12.7, §12.8). The skill gets a thin addendum (Phase 7) pointing Step 4+ at `dmctl` instead of hand-editing `create_drafts.py`. |
| `browser-helper` | research/enrichment ONLY (see §6.2 — ToS judgment) |
| `dashboard-reply-triage` skill | its `triage-*.json` output is ingestable via `dmctl ingest-triage` |

---

## 3. Data model (SQLite, full schema)

### 3.1 Migration approach

- Numbered SQL files in `data/migrations/`: `001_init.sql`, `002_triggers.sql`, `003_views.sql`. Committed to git.
- `db.migrate(conn, migrations_dir)` reads `schema_migrations`, applies missing files in numeric order, each inside its own transaction, records `(version, applied_at)`.
- Applied migrations are IMMUTABLE — schema changes are always a new numbered file. A test asserts fresh-install schema == incrementally-migrated schema (compare `sqlite3 dm.db .schema` dumps).
- `PRAGMA foreign_keys = ON` on every connection; `PRAGMA journal_mode = WAL` set once at init.

### 3.2 `001_init.sql` — tables (complete)

```sql
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
```

### 3.3 `002_triggers.sql` — invariants enforced INSIDE the database

These make the gates hold even against direct `sqlite3` writes. Owner A includes them verbatim; Owner D's `state.py` is the friendly layer on top.

```sql
-- G1: messages are born COMPOSED. Nothing can be inserted pre-approved/sent.
CREATE TRIGGER trg_msg_born_composed BEFORE INSERT ON messages
WHEN NEW.state != 'COMPOSED'
BEGIN SELECT RAISE(ABORT, 'G1: messages must be inserted in state COMPOSED'); END;

-- G2: APPROVED only from READY.
CREATE TRIGGER trg_msg_approve_path BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'APPROVED' AND OLD.state != 'READY'
BEGIN SELECT RAISE(ABORT, 'G2: APPROVED requires prior state READY'); END;

-- G3: STAGED only from APPROVED (the export gate).
CREATE TRIGGER trg_msg_stage_path BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'STAGED' AND OLD.state != 'APPROVED'
BEGIN SELECT RAISE(ABORT, 'G3: STAGED requires prior state APPROVED'); END;

-- G4: SENT only from STAGED, and only with evidence.
CREATE TRIGGER trg_msg_sent_path BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'SENT' AND (OLD.state != 'STAGED' OR NEW.sent_evidence IS NULL)
BEGIN SELECT RAISE(ABORT, 'G4: SENT requires STAGED + sent_evidence'); END;

-- G5: READY requires a lint_report whose "ok" is true.
CREATE TRIGGER trg_msg_ready_lint BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'READY'
 AND (NEW.lint_report IS NULL OR json_extract(NEW.lint_report, '$.ok') != 1)
BEGIN SELECT RAISE(ABORT, 'G5: READY requires passing lint_report'); END;

-- G6: Message-1 content gate (belt atop lint): no URL, no product name.
CREATE TRIGGER trg_msg1_no_pitch BEFORE UPDATE OF state ON messages
WHEN NEW.state IN ('READY','APPROVED') AND NEW.sequence_step = 1 AND (
     NEW.body LIKE '%http://%'  OR NEW.body LIKE '%https://%'
  OR NEW.body LIKE '%www.%'     OR NEW.body LIKE '%brightflowcure%'
  OR NEW.body LIKE '%BDAI%'     OR NEW.body LIKE '%bright desk ai%')
BEGIN SELECT RAISE(ABORT, 'G6: Message 1 contains link or product name'); END;

-- G7: Message-2 reply gate: step 2 cannot be APPROVED without a human reply.
CREATE TRIGGER trg_msg2_reply_gate BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'APPROVED' AND NEW.sequence_step = 2
 AND NOT EXISTS (SELECT 1 FROM replies r
                 WHERE r.prospect_id = NEW.prospect_id AND r.is_human = 1)
BEGIN SELECT RAISE(ABORT, 'G7: Message 2 requires a recorded human reply'); END;

-- G8: no message row may ever reference a suppressed contact.
CREATE TRIGGER trg_msg_suppressed BEFORE INSERT ON messages
WHEN EXISTS (
  SELECT 1 FROM contacts c
  JOIN suppression s ON (s.channel = c.channel OR s.channel = 'all')
  WHERE c.id = NEW.contact_id
    AND ( (s.scope = 'address' AND s.address = lower(c.address))
       OR (s.scope = 'domain'  AND c.address LIKE '%' || s.address)
       OR (s.scope = 'person'  AND s.address = c.person_key) ))
BEGIN SELECT RAISE(ABORT, 'G8: contact is on the suppression list'); END;

-- G9: email staging requires PASS, or RISK from a verified (non-guessed) source.
CREATE TRIGGER trg_email_verify_gate BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'STAGED' AND NEW.channel = 'email'
 AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = NEW.contact_id
     AND ( c.verify_verdict = 'PASS'
        OR (c.verify_verdict = 'RISK'
            AND c.address_source IN ('website','reply','linkedin','manual')) ))
BEGIN SELECT RAISE(ABORT, 'G9: email not PASS-verified (or RISK from format guess)'); END;

-- G10: append-only audit log.
CREATE TRIGGER trg_statelog_no_update BEFORE UPDATE ON state_log
BEGIN SELECT RAISE(ABORT, 'G10: state_log is append-only'); END;
CREATE TRIGGER trg_statelog_no_delete BEFORE DELETE ON state_log
BEGIN SELECT RAISE(ABORT, 'G10: state_log is append-only'); END;

-- G11: a second Message-1 to the same prospect is forbidden (dup-touch guard).
CREATE TRIGGER trg_one_msg1_per_prospect BEFORE INSERT ON messages
WHEN NEW.sequence_step = 1 AND EXISTS (
  SELECT 1 FROM messages m WHERE m.prospect_id = NEW.prospect_id
    AND m.sequence_step = 1
    AND m.state NOT IN ('LINT_FAILED','REJECTED','CANCELLED'))
BEGIN SELECT RAISE(ABORT, 'G11: prospect already has an active Message 1'); END;
```

### 3.4 `003_views.sql` — the measurement sheet (the report's four columns, one query)

```sql
CREATE VIEW sheet AS
SELECT
  m.variant_id                                   AS variant,
  c.address                                      AS sent_to,
  CASE WHEN EXISTS (SELECT 1 FROM replies r
                    WHERE r.prospect_id = m.prospect_id AND r.is_human = 1)
       THEN 'y' ELSE 'n' END                     AS replied,
  CASE WHEN o.outcome IN ('demo_booked','call_booked')
       THEN 'y' ELSE 'n' END                     AS booked,
  -- context columns (allowed extras; the four above are the contract)
  p.company_name, p.market, m.channel, m.sent_at,
  CASE WHEN m.state = 'BOUNCED' THEN 1 ELSE 0 END AS bounced
FROM messages m
JOIN prospects p ON p.id = m.prospect_id
JOIN contacts  c ON c.id = m.contact_id
LEFT JOIN outcomes o ON o.prospect_id = m.prospect_id
WHERE m.sequence_step = 1
  AND m.state IN ('SENT','REPLIED','BOUNCED','CLOSED');
```

`dmctl sheet` = `SELECT variant, sent_to, replied, booked FROM sheet` (+ `--full` for context columns, `--csv` to export). This IS the report's sheet.

---

## 4. The variant experiment engine

### 4.1 Variant definition & versioning

- Variants live in the `variants` table, seeded from the committed file `data/variants/EXP-2026-08-skeptic.json` by `dmctl init`.
- A variant row is **immutable once referenced by any message** (enforced in `variants.py`: edits create `V1.2` and retire `V1.1`). Stats always attach to the exact version sent, so copy tweaks never contaminate history.
- `spec` JSON per variant:

```json
{
  "structure": ["pain_opener", "soft_question"],
  "required_tokens": ["first_name", "company_casual", "hook_fact"],
  "opener_guidance": "…per-variant, see §5.2…",
  "soft_question_examples": ["…", "…"],
  "email_adaptation": "expand to <=120 words: hook para, pain bridge, question",
  "dm_constraint": "<=3 sentences, <=300 chars, no greeting line needed"
}
```

### 4.2 Seed experiment: `EXP-2026-08-skeptic`

Hypothesis (from the report): *the ICP is skeptical because they already bought a cheaper answer (answering service, chatbot, "we'll just use voicemail") and got burned; pain-first messages that name that experience out-earn generic missed-call messages.*

Five families (full copy guidance in §5.2): V1 burned-before, V2 missed-call-math, V3 off-the-tools, V4 after-hours, V5 competitor-quietly-wins.

### 4.3 Deterministic, reproducible assignment (`sorted-hash-rr-v1`)

Random-at-send-time is banned. Assignment happens once, at `dmctl assign`, and is a pure function of recorded inputs:

1. Inputs: `experiment_id`, the ordered list of `active` variant ids (recorded as `variant_set_hash`), the batch's member `prospect_key`s, and `batch_seq`.
2. For each prospect compute `assign_hash = sha256(experiment_id + '|' + prospect_key)`.
3. Sort the batch's prospects by `assign_hash` (stable, deterministic).
4. Assign variants round-robin in that sorted order, starting at offset `batch_seq % len(variants)` — the offset rotates the starting variant so small batches don't systematically favor V1, while staying fully deterministic.
5. Persist one `assignments` row per prospect with algo name, both hashes, batch id.

Properties: reproducible (re-running `assign` for the same batch is a no-op that verifies identity — mismatch aborts loudly), balanced within ±1 per batch, tamper-evident (hashes recorded), and stable per prospect (the `UNIQUE(experiment_id, prospect_id)` constraint means a prospect re-imported in another market/batch keeps the original variant — see edge cases).

If the active variant set changes mid-experiment (a variant version bump): already-assigned prospects keep their assignment; new assignments use the new set and record the new `variant_set_hash`. Stats are computed per exact variant id; `dmctl stats --by-family` aggregates versions within a family with a footnote line showing the per-version split.

### 4.4 Honest statistics

Definitions (fixed, to prevent metric gaming — see adversarial (h)):

- `sends(v)` = count of step-1 messages in `SENT|REPLIED|BOUNCED|CLOSED` for variant v.
- `delivered(v)` = sends(v) − bounced(v).
- `replies(v)` = prospects with ≥1 `is_human=1` reply. Bounces, auto-replies, vacation responders are **never** replies.
- `reply_rate(v)` = replies(v) / delivered(v). (Also print replies/sends as "rate incl. bounces" so the denominator choice is visible, not hidden.)
- `booked(v)` = prospects with outcome `demo_booked` or `call_booked` attributed per §8.
- `booking_rate(v)` = booked(v) / delivered(v).

Reporting tiers (what `dmctl stats` prints):

| Sample size (per variant) | Output |
|---|---|
| n < 30 | Raw counts ONLY (sends/delivered/replies/booked) + banner: `SAMPLE TOO SMALL — counts, not conclusions. No winner is declarable below n=30 sends AND 5 total human replies. The real n=20 signal is the reply text: run dmctl stats --replies.` |
| 30 ≤ n < 100 | Counts + reply-rate with Wilson 95% interval per variant. A variant may be flagged `leading` ONLY if its Wilson lower bound exceeds another's Wilson upper bound (disjoint intervals). Overlap prints `no separable signal yet`. |
| n ≥ 100 | Above + booking-rate Wilson intervals + per-market split for any cell with ≥30 delivered. |
| n ≥ 500 | Above + per-channel and per-market segment tables; optional two-proportion z-test note printed with the caveat that multiple comparisons across 5 variants inflate false positives (Bonferroni note: p×10 pairwise comparisons). |

Minimum-sample gate, mechanically enforced: `dmctl variants retire <id>` and `dmctl variants declare-winner <id>` REFUSE unless `delivered(v) ≥ variants.min_n_gate` (default 30) AND total human replies in the experiment ≥ 5. Override requires `--force --reason "<text>"` which writes a `state_log` row (`actor='operator'`) — the override exists but leaves a permanent audit trail.

Wilson interval implementation (Owner B, `stats.wilson(k, n, z=1.96) -> (lo, hi)`) — closed-form, stdlib math only; returns `(0.0, 1.0)` for n=0.

At n=20 total (~4/variant) the honest deliverable is: the sheet (SC-2) + `dmctl stats --replies` (verbatim reply snippets grouped by variant) + raw counts. The plan explicitly forbids the engine from printing any percentage without its n alongside it (lint on report.py output format; adversarial (h)).

---

## 5. Message generation

### 5.1 Division of labor

- **Copy is written by the `outreach-specialist` agent** (standing instruction: never inline), fed by a compose-job JSON the engine emits (`dmctl compose --batch B` → `data/review/B-…-composejob.json`). The job embeds: prospect facts, `company_casual`, `hook_fact` candidates, assigned variant's `spec`, channel constraints, banned-phrase list, and the agent-side sequence the operator already mandates: draft → `/voice-extractor` → `/de-ai-ify` → return.
- **The engine trusts nothing back.** Agent output re-enters via `dmctl ingest-drafts <file.json>` and every message passes `lint.py` mechanically. The agent pipeline is quality; the lint is law.

### 5.2 The skeptic-first angle, encoded (seed variant copy guidance)

All five: lead with THEIR pain, zero pitch, zero links, zero product name, soft question close, sign `the operator`. DM form ≤3 sentences. Email form ≤120 words, lowercase subject unique per prospect.

- **V1 burned-before** — angle: they already tried the cheap fix. Opener guidance: name the graveyard without naming products — "most {trade} owners I talk to have already tried an answering service or some chatbot for the phones, and it made things worse, not better." Soft Qs: "did you end up back at voicemail too?" / "what did you land on after that?"
- **V2 missed-call-math** — angle: mid-install economics. "you're two hours into a paver base and the phone buzzes twice — by the time you're off the compactor, that homeowner has already reached the next {trade} company on the list." Soft Q: "how many of those do you figure slip by in a week?"
- **V3 off-the-tools** — angle: the scaling identity. "getting off the tools was supposed to mean running the business — but the phone still runs you." Soft Q: "is the phone the last thing keeping you on-call?"
- **V4 after-hours** — angle: when homeowners actually call. "the {market} homeowners planning a backyard call at 7pm after their own workday — right when your day ends too." Soft Q: "what happens to the calls that come in after you knock off?"
- **V5 competitor-quietly-wins** — angle: the invisible loss + review hook. "{hook_fact — e.g. '212 five-star reviews'} says the work sells itself — but the lead you never heard ring never sees the work." Soft Q: "do you ever find out about the ones that didn't leave a voicemail?"

These are guidance, not templates — the copywriting agent writes fresh copy per prospect inside the variant's angle (satisfies cadence rule 4 "fully personalized unique bodies" → 30/day ceiling class).

### 5.3 Personalization tokens — sourcing and the missing-token failure mode

Tokens: `first_name` (from enrichment; fallback below), `company_casual` (from `/casualize-names` output, stored on the prospect, shown in the review file for human eyes), `hook_fact` (one concrete, verifiable fact: review count, years in business, a named project/award, service specialty — sourced by lead-sourcing enrichment; stored in `icp_fit_note`/compose job), `market` (city only, casualized: "Boise" not "Boise ID").

Missing-token handling (a top failure mode — a literal `{first_name}` shipping is worse than no message):

1. `compose.py` refuses to emit a compose job for a prospect missing `company_casual` or `hook_fact` — that prospect is listed in the job file under `"blocked": [...]` with the missing fields, and stays un-messaged.
2. `first_name` missing → the variant spec's `no_name_open` alternative is used (openers above all work name-free); the compose job marks `"first_name": null` so the agent writes name-free copy. A greeting like "Hey there" is banned (lint).
3. `lint.py` hard-fails any body/subject containing `{`/`}` placeholders, `[`/`]` bracket placeholders, the literal strings `first_name`/`company_name`, an empty first line, or a hook shorter than 20 characters.

### 5.4 Mechanical enforcement (`lint.py` — full rule list)

Input normalized first: Unicode NFKC, zero-width chars stripped, curly quotes folded — then rules run on the normalized text (defeats homoglyph smuggling; adversarial (l)).

| # | Rule | Applies to |
|---|---|---|
| L1 | No URLs: regex `https?://|www\.|\b\S+\.(com|net|org|io|ai)/\S*` and bare `domain.tld` followed by `/` | step-1 body+subject, all channels |
| L2 | No product identity: `bdai`, `bright desk ai`, `brightflow`, `brightflowcure` (case-insensitive, after normalization) | step-1 |
| L3 | Banned phrases (case-insensitive): `i'd love to connect`, `synergy`, `circle back`, `touch base`, `just checking in`, `hope this finds you well`, `quick question` (as subject), `revolutionize`, `game-chang`, `cutting-edge`, `streamline`, `solution` (step-1 only), `hey there` | all |
| L4 | No emojis (any codepoint in Emoji ranges) | all outbound |
| L5 | Unresolved placeholders: `{`, `}`, `[`, `]`, literal `first_name`, `company_name`, `hook_fact` | all |
| L6 | Email: body ≤120 words; subject present, non-empty, entirely lowercase, ≤60 chars; signature line exactly `the operator` (no title, no phone, no links) | email |
| L7 | LinkedIn DM: ≤3 sentences (split on `.!?` with abbreviation guard), ≤300 chars, no subject | linkedin |
| L8 | Exactly one question mark in the closing sentence (one CTA rule) | step-1 |
| L9 | Subject uniqueness: subject must not equal (case-folded) any other subject in the same batch OR any subject staged in the last 30 days | email |
| L10 | Casualization sanity on `company_casual`: no `LLC`/`Inc`/`Corp`/`Ltd`, not ALL-CAPS, ≤4 words, no trailing punctuation | all |
| L11 | Step-2 only: body must contain `brightdesk.brightflowcure.com/demo-ai-voice` exactly once (imported constant `DEMO_LINK` from `create_drafts.py`), and greeting must use the prospect's real first name if known | step-2 |
| L12 | Fake-urgency markers: `act now`, `limited time`, `last chance`, `don't miss` | all |

`lint_message()` returns a `LintReport` (§12.4). Any violation → state `LINT_FAILED`, report stored in `messages.lint_report`, violation list printed. LINT_FAILED messages can be re-ingested (new body) — they re-enter at COMPOSED.

---

## 6. Channel adapters

Uniform interface (§12.5) so the core is channel-agnostic. Two adapters ship in v1.

### 6.1 Email (`channels/email_gmail.py`) — primary channel

- **Stage:** for each APPROVED email message (in one flock'd cap-checked transaction, §10c): re-check suppression + verify verdict (G8/G9 also enforce), then `gmail_compat.get_service()` → `create_draft(service, to, subject, body)` (the real function from `create_drafts.py`); store `gmail_draft_id`; transition `APPROVED→STAGED`. `--dry-run` writes the would-be draft as JSON to `data/outbox/dry-run/` and changes NO state.
- **Pre-stage dedup (belt atop G11):** call `already_sent_to(service, address)` — the Gmail-Sent authoritative check from `create_drafts.py` (TICKET-694 lesson). Hit → skip + warn, message → `CANCELLED` with reason.
- **Verification:** `dmctl verify` runs `verify_batch(addresses, smtp=True)` via `gmail_compat` and writes verdicts to `contacts`. On WSL, SMTP probes can false-FAIL/RISK because the residential IP is Spamhaus-PBL-listed (documented in `create_drafts.py` and handled inside `verify_emails.py` `_is_ip_block`); the CLI prints the standing advice: authoritative `--smtp` verification runs on the VPS (`ssh user@send-host.example`). Verdict + reason + timestamp stored; RISK policy is G9.
- **Sending: NOT THIS SYSTEM.** The engine contains zero calls to `messages().send`, `drafts().send`, or `smtplib` sends (tested by grep — adversarial (a)). Real sends happen via (preferred) the DASHBOARD EXEC send-queue — engine-staged drafts appear in its `list-drafts`, the operator APPROVEs + ARMs, EXEC's server loop enforces 10/window + 20/day + stagger and runs `gmail_send_queue.py send --draft-id X --confirm-send` on the VPS — or via Gmail native schedule-send by hand. `scheduledSendTime` does not exist in the Gmail API and appears nowhere in this design.
- **Reconcile:** `dmctl reconcile` (read-only Gmail, safe on any host) scans `in:sent to:<address>` newer than staging time → `STAGED→SENT` with `sent_evidence='gmail_sent_scan:<msgid>'`; scans mailer-daemon returns → `SENT→BOUNCED` + auto-suppress (`reason='bounce'`) + append address to `~/.config/skills/gmail-helper/suppression_list.txt` under the engine's marker comment.
- Cost: $0. Risk: sender reputation — mitigated by the verify gate, suppression, caps, bounce auto-suppression.

### 6.2 LinkedIn (`channels/linkedin_manual.py`) — research-automated, human-sent

**Explicit tradeoff (decided here, builders do not revisit):** automated LinkedIn DM sending violates LinkedIn's ToS (User Agreement §8.2 prohibits bots/scrapers/automated access) and LinkedIn actively bans accounts for it. The operator has ONE personal LinkedIn account; losing it kills a channel permanently to save seconds per message. Therefore:

- **Allowed automation (browser-helper, read-only):** profile/company enrichment and finding where the ICP posts — via `universal_scraper.py` / `linkedin_scraper.py` with the human-behavior layer (gaussian delays, 40–55 WPM typing, break cadence per SKILL.md). Engine-side policy: ≤25 profile views/day, ≤3 sessions/day, hard stop on any checkpoint/captcha page (mark contact `needs manual enrichment`, never retry same day). Cookie (`LINKEDIN_LI_AT`) lives in browser-helper's own `.env`, never in dm-engine.
- **Sending is a human-send queue:** staging an APPROVED LinkedIn message writes (1) a card file `data/outbox/linkedin/<message_id>-<slug>.md` containing the profile URL, the exact message text in a fenced block for one-click copy, and a `dmctl mark-sent <id>` line; (2) a DASHBOARD approvals item (exec-card shape) titled `LinkedIn DM ready: <company>` linking the card. The operator opens the profile, pastes, sends, runs `dmctl mark-sent <id> --evidence "sent manually <date>"` → `STAGED→SENT`. Reply recording is likewise manual: `dmctl record-reply <prospect> --channel linkedin --class hot --snippet "..."`.
- Cost: $0 + ~60 seconds of the operator per DM. That minute is the price of keeping the account; at 10 DMs/day it is 10 minutes — acceptable at this stage.

### 6.3 Channels considered and deferred (explicit cost/risk notes)

- **Instagram DM:** many hardscape contractors are more active on IG than email. Same ToS/ban calculus as LinkedIn → would be another human-send queue. Schema already supports it (`contacts.channel='instagram'`). Deferred to keep v1 small (non-goal §14); adding it later = one new adapter file cloning `linkedin_manual.py`.
- **Cold SMS: excluded entirely.** TCPA requires documented express written consent; cold SMS to scraped numbers is illegal exposure, not a growth channel. The `sms` enum value exists only so consent-based SMS could be added later; no adapter, and `cli.py` rejects `--channel sms` with the TCPA reason.
- **Paid options flagged (all rejected for $0 constraint):** Apollo email credits (free tier may return no emails — engine treats Apollo-sourced addresses as `address_source='apollo'` and they still face the SMTP gate), Instantly/Smartlead ($37+/mo), phone-validation APIs. None required.

---

## 7. The approval gate

### 7.1 Principle

**The engine cannot send. Approval gates what it CAN do (stage), and the send layer has its own second human gate.** Two independent human clicks stand between a composed message and a prospect's inbox:

1. Engine approval (the operator marks the review file / interactive TTY confirm) — gates content leaving the DB into Gmail-drafts or the LinkedIn outbox.
2. Send release (the operator sends in Gmail, or APPROVE+ARM in the DASHBOARD EXEC queue, or physically pastes a LinkedIn DM) — outside the engine, already double-gated in EXEC (the external send-queue: "TWO human clicks — per-draft APPROVE + explicit ARM").

For LinkedIn the human IS the transport, so gate 2 is inherent.

### 7.2 State machine (messages)

```
                    ┌────────────► LINT_FAILED ──(re-ingest new body)──► COMPOSED
COMPOSED ──lint──► READY ──approve──► APPROVED ──stage──► STAGED ──external send──► SENT
   │                 │                    │                                   │
   │                 └──reject──► REJECTED│                                   ├─► BOUNCED
   └──cancel──► CANCELLED ◄───────────────┘ (un-approve before staging)       ├─► REPLIED
                                                                              └─► CLOSED
```

Legal transitions are the ONLY ones `state.transition()` permits, and triggers G1–G7 re-enforce the load-bearing edges at the SQL layer. Every transition writes `state_log(actor, reason, evidence)`.

### 7.3 The approval mechanism

- `dmctl review --batch B` writes `data/review/<batch>-review.md`: one block per READY message showing recipient, verify verdict + source, variant id, subject, full body, the tokens used (including `company_casual` for human sanity check), and a line `- [ ] approve` / `- [ ] reject: <reason>`.
- The operator edits checkboxes (or writes `approve-all: yes` in the header to take the whole batch).
- `dmctl approve --from-review data/review/<file>` parses marks and transitions only checked messages `READY→APPROVED`, recording `approved_via='review_file:<path>'`, `actor='operator'`. It verifies the review file's mtime is later than the newest `updated_at` of the messages it covers (a stale file — messages re-composed after review — aborts).
- Interactive alternative: `dmctl approve --batch B` on a TTY shows each message and requires typing `APPROVE`. **If stdin is not a TTY and no `--from-review` is given, the command refuses** — a headless agent cannot approve by flag. There is no `--yes`, no `--force`, no env override on this command, by design.

### 7.4 No-bypass proof obligations (each is a test in `tests/adversarial/test_gates.py`)

| Path | Why it can't bypass | Test |
|---|---|---|
| CLI flags | `stage` refuses non-APPROVED (code) + G3 trigger; `approve` has no non-interactive flag path; grep test asserts no `--yes`-style bypass strings exist in `cli.py` | drive CLI on a fixture DB: `stage` on READY message → nonzero exit, state unchanged |
| Direct DB write | G1 (can't insert pre-approved), G2/G3/G4 (can't jump states) — raw `UPDATE messages SET state='STAGED'` on a READY row raises ABORT | execute raw SQL, assert `sqlite3.IntegrityError`/`OperationalError` with `G3` in message |
| Export path (Gmail drafts) | `email_gmail.stage()` selects `WHERE state='APPROVED'` only; even if handed a row, the G3 transition inside the same transaction aborts staging non-approved | call `stage()` with a forged row list containing a READY id → 0 drafts created (FakeGmail), exception logged |
| Export path (LinkedIn cards) | identical structure in `linkedin_manual.stage()` | same test shape |
| Dry-run | writes files to `outbox/dry-run/`, performs zero transitions, creates zero Gmail objects | assert DB unchanged + no FakeGmail calls after `--dry-run` |
| Send | there is no send code (§10a grep test) | grep test |
| Review-file forgery/staleness | file must postdate message updates; approve logs the file path + sha256 of its content into `state_log.evidence` so any approval is traceable to an exact reviewed text | stale-file test → abort |

Residual risk stated honestly: someone with a shell can always edit source or delete triggers. The threat model is *accidental/agent bypass*, not a malicious local root — same stance as `_sender_host_guard.py` (an env var overrides it, but the override is loud and deliberate).

---

## 8. Measurement + attribution

### 8.1 Reply detection (`replies.py`)

- `dmctl scan-replies` (read-only Gmail, any host): for every SENT/STAGED→SENT email message, search `from:<address>` (the `has_inbound_reply` pattern from `create_drafts.py`, which correctly excludes Sent mail) and fetch new messages by `gmail_message_id` dedup. Prefer thread-match: if the inbound `threadId` equals the stored `gmail_thread_id`, bind `reply.message_id`; else bind by address at prospect level (`message_id NULL` — still counts for the sheet, which is prospect-scoped).
- Classification, two layers: (1) **heuristics** in code — `mailer-daemon@`/`postmaster@` senders or `Delivery Status Notification` subjects → `bounce`; `Auto-Submitted: auto-replied`/`auto-generated` header, `X-Autoreply`, or subject matching `out of office|auto.?reply|vacation|away until` → `auto_reply`/`vacation` (`is_human=0`); everything else → `unsure`, `is_human=1`. (2) **intent** — ingest `dashboard-reply-triage` output (`dmctl ingest-triage ~/work/dashboard/data/approvals/triage-*.json`, matching its documented item schema: `id/kind/from/classification/summary/draftId/...`) or `dmctl record-reply … --class hot|question|objection|no`. A `no` classification auto-creates a suppression row (`reason='hard_no'`) and sets outcome `replied_no`; explicit unsubscribe wording → `reason='unsubscribe'`, outcome `unsubscribed`.
- Transition `SENT→REPLIED` on first `is_human=1` reply.

### 8.2 Demo-booking attribution — reasoning about the join key

The demo form collects: first name, phone, company name, company type, company website. **It does not collect email — so there is no natural exact join key** between an outreach row (keyed by email/domain) and a GHL contact created by the form. Design, in confidence order:

1. **`utm` (exact — mechanism we create):** Message 2's demo link becomes `https://brightdesk.brightflowcure.com/demo-ai-voice?utm_source=outreach&utm_medium=<channel>&utm_content=<prospect_key>`. GHL forms capture UTM parameters into contact attribution when present. Same URL, satisfies L11 (the lint matches the bare `DEMO_LINK` substring). If GHL drops the params, nothing is lost — fall through. (Open question Q1 confirms the capture; default = ship the params.)
2. **`domain_match` (high):** normalize the form's "company website" (strip scheme/`www`, lowercase, strip path) and equality-match `prospects.domain`. This is the strongest natural key — the demo form requires the website and we required it at prospecting time.
3. **`name_phone_match` (high):** normalized company name match (lowercase; strip punctuation and `llc|inc|co|corp|ltd|landscap\w*|hardscap\w*` suffix noise) AND (first_name match OR phone match when we have one).
4. **`manual_confirm`:** anything ambiguous becomes a DASHBOARD approvals card — "Demo form from <company>: is this prospect #123 (V3, Boise)?" — the operator confirms with `dmctl book <prospect_id> --method manual_confirm --evidence <ghl_contact_id>`. At current volumes (bookings/week ≈ single digits) this is minutes, not hours.

Time window: only match GHL contacts created after the prospect's Message-2 `sent_at` and within 45 days; a match writes `outcomes(outcome='demo_booked', attribution_method, attribution_confidence, ghl_contact_id, evidence)`.

### 8.3 Reading GHL (given the measured mess)

Measured 2026-08-01: all 30 open opportunities have `monetaryValue: 0` (revenue attribution from GHL impossible — out of scope), nothing has ever been Closed Won, and `search_opportunities` at limit=100 returns ~61,500 chars (context overflow). Therefore:

- The engine never calls GHL live. An agent step (interactive session or the `dashboard-metrics-pull` pattern) exports the needed slice to disk: `dmctl attrib` consumes `--ghl-export <file.json>` — a JSON array of contacts `{id, firstName, companyName, website, phone, dateAdded, tags, source, attributions}` produced via `mcp__ghl__search_contacts` (paged, small limits, written to disk in chunks). Booking signal = form-submission contact (source/tag) or an appointment (`get_contact_appointments`) — NOT opportunity monetaryValue, NOT Closed Won.
- The export step is documented in `docs/` runbook (Phase 6) as a copy-paste agent prompt.

### 8.4 DASHBOARD metrics emit (`report.emit_dashboard_metrics`)

Writes `~/work/dashboard/data/metrics/pull-<YYYY-MM-DD-HHmm>.json` per the contract (plain numbers, omit unavailable, never delete old files):

```json
{ "generatedAt": "<ISO>",
  "metrics": {
    "dm.sends_total": 20, "dm.sends_7d": 12,
    "dm.replies_human_total": 3, "dm.reply_rate_delivered_pct": 15.8,
    "dm.demos_booked_total": 1, "dm.attempts_gap_to_20": 0,
    "dm.pending_approval": 4, "dm.staged_unsent": 6, "dm.suppressed_total": 17
  } }
```

`dm.*` keys are new — they coexist with `ghl.*` keys from other pulls; the DASHBOARD server merges by key. Rates rounded to 1 decimal; `reply_rate_delivered_pct` omitted (not 0) when delivered=0.

---

## 9. Edge cases (by subsystem)

### Prospect intake / identity
1. **Duplicate prospect across markets/batches:** `prospect_key` (domain) is UNIQUE — re-import is an update-not-insert (`ImportResult.dupes`), original market kept, new market appended to `source_detail`. Variant assignment survives (UNIQUE on assignments). G11 blocks a second Message 1.
2. **Same human at two companies:** `contacts.person_key` (normalized email / LinkedIn slug) is shared across prospect rows. Pre-stage check: any SENT message to the same `person_key` in 90 days → staging skips with a review-file warning (`held: same person messaged <date> via <company>`); the operator may release with `dmctl stage --allow-person <person_key>` (logged).
3. **Prospect with no website:** `prospect_key='name:<company-slug>:<market-slug>'`, `domain` NULL, domain-based attribution unavailable (falls to name match) — flagged in review file.
4. **Two companies share one domain** (parent brand): second import of the same domain with a different company name aborts with both names printed; human resolves via `--force-key`.
5. **Franchise/excluded ICP slips in:** `dmctl exclude <id> --reason` → `EXCLUDED` (terminal); message insertion for excluded prospects blocked in code (prospects must be QUALIFIED to enter a batch).
6. **Company name reads badly casualized** ("PHX Pvrs LLC" → "phx pvrs"): L10 lint catches structural badness; the review file always displays `company_casual` in context so a human reads every one before approval; fix = `dmctl set-casual <id> "<name>"` then re-compose.
7. **Non-US prospect scraped by accident:** `us_state` required at import; unmapped state string → import row rejected with error listed in `ImportResult.errors`.

### Verification / email hygiene
8. **RISK verdict:** stageable only when `address_source ∈ {website, reply, linkedin, manual}` (G9) — i.e., a human saw it somewhere real; `format_guess`/`apollo`/`directory` RISK is blocked. Matches standing rule 6.
9. **FAIL verdict:** never stageable (G9); contact kept (audit) but review file lists it under `dropped — fallback to LinkedIn` mirroring the email-outreach Step 3.5 policy.
10. **WSL false-FAILs (Spamhaus PBL):** `verify_emails.py` already returns RISK-not-FAIL on IP-block markers (`_is_ip_block`); `dmctl verify` prints the VPS re-verify advice whenever any RISK reason contains `blocklist`. Verdicts from a VPS run can be imported: `dmctl verify --import <json>`.
11. **Verification staleness:** verdicts older than 30 days at stage time → warning + re-verify prompt (domains die between prospecting and sending).
12. **Bounce after send:** reconcile detects mailer-daemon → `BOUNCED`, auto-suppress, append to the skill's `suppression_list.txt` (the format documented in that file's header: lowercase, one per line, comment groups), and the sheet keeps the row with `replied='n'` while stats exclude it from the delivered denominator.

### Compose / lint
13. **Missing personalization token:** §5.3 — blocked pre-compose, never ships. Literal `{token}` → L5 hard fail.
14. **Agent returns fewer drafts than prospects:** ingest reports `missing: [...]`; batch stays `COMPOSED` until complete or prospects are explicitly dropped (`dmctl drop-from-batch`). No silent truncation (adversarial (i)).
15. **Agent returns a draft for a prospect not in the batch:** rejected by ingest (unknown prospect_key) — prevents cross-batch contamination.
16. **Duplicate subject within batch or vs last 30 days:** L9 fails that message only; re-compose it.
17. **Emoji / homoglyph / zero-width smuggling:** NFKC + strip pass before lint (L4, adversarial (l)).
18. **Body legitimately needs a URL-looking string** ("their site azpavers.com is down" hook): forbidden anyway in step-1 (L1 has no whitelist — the rule is absolute per the framework); the compose job instructs agents to reference "your website" without the literal domain.

### Assignment / experiment
19. **Variant retired mid-batch:** assignments already made keep the retired variant (immutability); `assign` for new batches uses the current active set; `stats --by-family` aggregates.
20. **Re-running `assign` on an assigned batch:** recomputes and compares — identical = no-op; different (variant set changed) = loud abort telling the operator to open a new batch. Never silent reassignment (adversarial (g)).
21. **Batch with fewer prospects than variants (e.g. 3):** round-robin from the rotating offset still covers 3 distinct variants; balance emerges across batches via `batch_seq` offset.

### Approval / staging
22. **Batch partially fails midway (Gmail 500 on draft #3 of 5):** each message stages in its own transaction; failures leave that message APPROVED (retryable), run recorded `partial` in `runs`, exit code 1, printed list of staged vs pending. Re-run resumes — idempotent because…
23. **Duplicate drafts from a re-run:** stage skips any message that already has `gmail_draft_id`; additionally lists current Gmail drafts (`drafts().list` + metadata, the `gmail_send_queue.py list-drafts` pattern) and skips if a draft to the same address+subject already exists (covers a crash after draft-create but before DB write); orphan cleanup prints `dmctl` guidance rather than auto-deleting.
24. **Gmail token expiry mid-batch (headless cannot re-auth):** catch `RefreshError` → abort remaining, run `partial`, exit code 4 (matching `create_drafts.py`'s exit-4 convention), and append `re-auth: python gmail_multi_auth.py --account operator_personal` to `~/.config/pending-actions.md` (standing ledger rule). Never retried blind (retry-cap rule).
25. **The operator edits a draft in Gmail before sending:** allowed and expected — reconcile records SENT from the Sent copy; if the sent subject/body differs from the DB copy, store the delta note in `sent_evidence` (`edited_in_gmail`) so stats footnote the variant purity at n where it matters.
26. **Draft deleted in Gmail without sending:** reconcile finds neither draft nor sent copy after 14 days → prompts: `dmctl cancel <id> --reason "draft discarded"` (goes to CANCELLED, leaves the cap ledger row — caps count staging, conservatively).
27. **Approval of an empty batch / already-approved messages:** no-op with explicit message, exit 0.

### Caps / clock / host
28. **Daily cap crossing midnight:** `phx_date` computed from fixed UTC-7 (`America/Phoenix` has no DST; matches the external send-queue's `PHX_OFFSET_MS`). A batch staged 23:50→00:10 Phoenix splits its cap accounting across two `phx_date`s naturally.
29. **Two concurrent runs racing the cap:** `locks.hold('stage')` (flock, blocking with 30s timeout) + `BEGIN IMMEDIATE`; the cap query (`SELECT count(*) FROM stage_events WHERE phx_date=? AND channel='email'`) and the insert happen inside one transaction. See adversarial (c).
30. **Cap arithmetic:** email staging caps at 20/`phx_date` when batch `personalization='template'`, 30 when `'unique'`; mixed days take the lower bound (if any template-class message staged that day, cap 20). Window pacing (10/window, 3–5 min stagger) belongs to the EXEC send-queue, which enforces it in code at send time.
31. **Prospect timezone (national ICP, 4+ zones):** `us_state → IANA tz` map in `prospects.py` (50 states + DC; AZ→America/Phoenix). Review file and LinkedIn cards print `suggested send window: 08:00–18:00 <tz> = HH:MM–HH:MM MST` so the operator arms EXEC due-times inside the prospect's business hours; `dmctl status` warns for any STAGED message whose EXEC-armed slot (if the operator supplies `--planned-time`) would land before 8am or after 6pm prospect-local. A 6am-local send is a deliberate act, never a default.
32. **WSL-vs-VPS split:** engine (drafting) runs anywhere — it cannot send. Read-only Gmail ops run anywhere. SMTP verification is authoritative only from the VPS (edge 10). Real sends only exist on the VPS via EXEC / `send_*.py` (which carry `require_sender_host()`). The DB lives on WSL; if a VPS process ever needs it, that is a Phase-later sync problem — explicitly out of scope (non-goal).

### Replies / outcomes
33. **Prospect replies after being marked dead:** reply scanner matches by address regardless of outcome; a human reply on a `dead`/`replied_no` prospect reopens it — outcome→`in_progress` (audit row), DASHBOARD card `prospect came back from the dead`. Suppressed prospects (hard_no/unsubscribe) do NOT reopen — the reply is recorded, but a card says `suppressed contact replied — the operator decides manually`; the engine will still refuse new messages to them (G8) unless the operator removes the suppression row deliberately.
34. **Unsubscribe / hard no:** suppression `scope='address'` + `reason`; `hard_no` also suppresses `scope='person'` when `person_key` known (they said no — their next company doesn't reset consent). Permanent: no expiry column exists on purpose.
35. **Auto-responder counted as reply:** impossible by definition — `is_human=0` classes are excluded from `replied` (sheet subquery filters `is_human=1`) and from reply-rate numerators (adversarial (h)).
36. **Reply arrives on a different address than we wrote to** (owner replies from personal Gmail): thread-match by `gmail_thread_id` catches it (same thread); recorded against the prospect with the observed address stored in snippet metadata; new address added as a `reply`-sourced contact (now PASS-equivalent for Message 2).
37. **Demo form filled by someone we never contacted** (organic): attribution finds no prospect match → not counted; appears only in GHL. The engine never claims organic demos (adversarial (h)).
38. **Two prospects match one GHL demo contact:** ambiguous → `manual_confirm` card; never auto-pick.

---

## 10. Adversarial checks (build-time obligations, each with reviewer verification)

| ID | Attack / failure | Concrete check the build implements | How the reviewer verifies it works |
|---|---|---|---|
| (a) | Any code path sends without approval | 1) Zero send capability: no `messages().send`, `.drafts().send(`, `smtplib.SMTP.sendmail`, or `send(` on a Gmail service object anywhere under `src/`; enforced by `tests/adversarial/test_gates.py::test_no_send_code` (AST/grep over the source tree, allowlist empty). 2) G1–G4 triggers. | Run the grep test; then hand-inject `service.users().messages().send` into a scratch copy and watch the test fail. Attempt `UPDATE messages SET state='SENT'` on a COMPOSED row in `sqlite3` — expect `G4` abort. |
| (b) | Message 1 ships containing a URL / product name | L1+L2 lint (normalized text) AND trigger G6 (SQL belt) — two independent layers | Ingest a fixture draft containing `brightdesk.brightflowcure.com` → LINT_FAILED; then bypass lint by raw-SQL updating body on a READY row and attempt READY→APPROVED → G6 abort. Both are test cases. |
| (c) | Daily cap exceeded by two concurrent runs | flock (`locks.hold('stage')`) + `BEGIN IMMEDIATE` + cap-check-and-insert in one transaction; cap counts `stage_events` per `phx_date` | `tests/test_locks.py::test_concurrent_stage`: two processes (multiprocessing) race 15+15 stages against cap 20 → total staged == 20, no exception swallowed. Reviewer also inspects that the count query and insert share a transaction. |
| (d) | Same prospect messaged twice | G11 trigger (one active Message-1 per prospect) + `already_sent_to()` Gmail-Sent check at stage + `UNIQUE(experiment_id, prospect_id)` assignment | Insert second step-1 message for same prospect via raw SQL → G11 abort. Stage a message to an address present in FakeGmail's sent store → skipped, CANCELLED. |
| (e) | Suppressed/unsubscribed contact re-contacted | G8 trigger at message INSERT + suppression re-check inside `stage()` + file-sync import before every stage run | Add address to suppression, attempt compose→ingest → insert aborts. Add to `suppression_list.txt` only (not DB), run stage → file sync imports it first, stage refuses. Both tested. |
| (f) | FAIL-verified address reaches a draft | G9 trigger + `stage()` re-verifies verdict freshness + `create_drafts.py`'s own `verify_recipients` hard-abort remains as the historical backstop for any legacy path | Set contact verdict FAIL, attempt stage → G9 abort, zero FakeGmail draft calls. |
| (g) | Variant assignment gamed / non-reproducible | Pure-function algo over recorded inputs; `assign` re-run compares and aborts on drift; `assign_hash`/`variant_set_hash` stored per row | `tests/test_variants.py::test_reassign_identical` (same inputs twice → byte-identical assignments) and `::test_tamper_detect` (mutate one assignment row, re-run assign → loud abort). Reviewer recomputes one hash by hand: `python -c "import hashlib; print(hashlib.sha256(b'EXP-2026-08-skeptic|acmehardscapeaz.com').hexdigest())"` and matches the row. |
| (h) | Metric overstatement (bounce as non-reply, wrong denominator, auto-responder as reply, organic demos claimed) | Fixed metric definitions in §4.4 implemented ONLY in `stats.py` (single source; `report.py` imports from it); bounces excluded from delivered denominator but PRINTED as their own column; `is_human=0` excluded from all reply numerators; attribution requires a stored method+evidence; every printed rate carries its n | `tests/test_stats.py` fixture: 10 sends, 2 bounces, 1 human reply, 1 auto-reply, 1 organic GHL contact → asserts reply_rate = 1/8, replies=1, booked=0. Reviewer greps `report.py` for any division not delegated to `stats.py`. |
| (i) | Silent truncation / dropped prospects | Every multi-row operation returns a typed result (`ImportResult`, `IngestResult`, `StageResult`) with explicit counts + per-item skip reasons, printed and stored in `runs.detail`; ingest refuses to close a batch with unaccounted prospects (edge 14) | Feed a 10-prospect file where 3 are suppressed/dupes → output lists all 3 with reasons; `runs.detail` JSON contains them; test asserts `added+dupes+skips+errors == input rows`. |
| (j) | Secrets leakage into logs/docs/commits | Engine holds NO secrets (Gmail tokens stay in the skill root, referenced by path; no GHL key; no LinkedIn cookie). `.gitignore` (committed before any `.env`) covers `data/dm.db*`, `data/review/`, `data/outbox/`, `.env`. `.env.example` committed with placeholder-only names. `tests/adversarial/test_gates.py::test_no_secrets` scans the repo for key signatures (`pit-`, `sk-`, `AKIA`, `ghp_`, `xox`, `AIza`, `sk_live_`, `whsec_`, `li_at=`) and asserts tracked files contain none; log writer redacts anything matching those prefixes. | Run the scan test; drop a fake `ghp_XXXX` into a tracked file → test fails. `git status` shows db/review/outbox untracked. |
| (k) | State tampering via direct DB writes to "un-send" or fake history | G10 (state_log append-only) + G1–G4; `sent_evidence` required for SENT | Attempt `DELETE FROM state_log` → abort. |
| (l) | Lint bypass via Unicode (homoglyph `fdаi` with Cyrillic а, zero-width joins in `brightflowcure`) | NFKC normalization + zero-width strip before every lint rule; trigger G6 catches the ASCII forms post-normalization writes (body stored normalized) | Fixture drafts with Cyrillic/ZWJ smuggled product names → LINT_FAILED. |
| (m) | Whole batch ships with one identical subject (cadence rule 1) | L9 uniqueness within batch + 30-day lookback | Fixture batch with two equal subjects → second fails lint. |
| (n) | Test suite touches real Gmail/GHL | `config.load_config()` raises unless `DM_ENGINE_DB` points inside the repo OR `DM_ENGINE_TEST=1`; under `DM_ENGINE_TEST=1` `gmail_compat.get_service()` raises `TestModeError` (tests must inject fakes); pytest `conftest.py` sets it globally | Delete a fake injection from a channel test → test errors with `TestModeError` instead of hitting the network. Reviewer confirms no test imports `googleapiclient` transitively (import-hook assertion in conftest). |
| (o) | Approval forged by agent (non-TTY) | §7.3: non-TTY without `--from-review` refuses; review-file sha256 recorded | Pipe `yes | dmctl approve --batch B` → refusal, exit 2. |

---

## 11. Build phases (ordered; each with definition-of-done + verification command)

All commands run from `the repo root` with `PYTHONPATH=src`. `dmctl` = `python -m dm_engine.cli`.

**Phase 0 — Walking skeleton (one prospect, end to end, before any breadth).**
Minimal versions of: config, db+migration 001–003, one-prospect import, trivial assign, hand-written draft ingest with lint stub (L1/L2/L5 only), review→approve, stage `--dry-run`, `mark-sent`, sheet.
DoD: one prospect flows NEW→…→SENT entirely via CLI with `--dry-run` staging; sheet returns exactly 1 row with 4 columns.
Verify:
```
python -m dm_engine.cli init
python -m dm_engine.cli import-prospects tests/fixtures/prospects/one.json --market "Boise ID"
python -m dm_engine.cli assign --experiment EXP-2026-08-skeptic --batch B-TEST-1
python -m dm_engine.cli ingest-drafts tests/fixtures/messages/one_ok.json
python -m dm_engine.cli review --batch B-TEST-1   # human edits checkbox
python -m dm_engine.cli approve --from-review data/review/B-TEST-1-review.md
python -m dm_engine.cli stage --batch B-TEST-1 --dry-run
python -m dm_engine.cli mark-sent 1 --evidence "phase0 smoke"
python -m dm_engine.cli sheet        # → 1 row
```

**Phase 1 — Data layer complete (Owner A).** Full schema, all triggers, migrations runner, locks, config guard.
DoD: `pytest tests/test_db.py tests/test_triggers.py tests/test_locks.py -q` green; fresh-vs-migrated schema dump identical; every G-trigger has a failing-write test.

**Phase 2 — Prospects, suppression, variants, stats (Owner B).**
DoD: `pytest tests/test_prospects.py tests/test_suppression.py tests/test_variants.py tests/test_stats.py -q` green; determinism + tamper tests pass; suppression file two-way sync round-trips against a fixture copy of `suppression_list.txt`.

**Phase 3 — Compose + full lint (Owner C).**
DoD: `pytest tests/test_lint.py tests/test_compose.py -q` green; all L1–L12 have positive AND negative fixtures; compose job JSON validates against contract §12.8.

**Phase 4 — Channels, state, approval (Owner D).**
DoD: `pytest tests/test_state.py tests/test_approval.py tests/test_channels.py -q` green against `tests/fakes.py`; real-Gmail smoke (manual, the operator present): stage ONE real draft to the operator's own address `sam.taylor@example.com`, see it in Gmail, delete it.
Verify smoke: `dmctl stage --batch B-SMOKE --limit 1` then eyeball Gmail drafts.

**Phase 5 — Replies, attribution, reporting, CLI complete (Owner E).**
DoD: `pytest tests/test_replies.py tests/test_attribution.py tests/test_report.py tests/test_cli.py -q` green; `dmctl metrics-emit` writes a contract-valid file into a temp dir under test; triage-file ingest works against a copied real `triage-*.json` shape.

**Phase 6 — Integration + adversarial gate.**
DoD: `pytest tests/ -q` fully green including `tests/adversarial/test_gates.py`; Phase-0 walkthrough re-run on the full system; GHL export runbook prompt written in `docs/RUNBOOK.md` (the only doc besides PLAN.md; keep it short).
Verify: `pytest -q` and the §11 Phase-0 command block.

**Phase 7 — Live pilot to SC-1 (the operator in the loop).**
Two batches × 10 prospects (one email-heavy, one including LinkedIn cards), through review → EXEC send-queue → reconcile → scan-replies → sheet. the earlier email pipeline SKILL.md gets an addendum: Steps 4–5 replaced by `dmctl ingest-drafts / review / approve / stage`; `create_drafts.py` untouched (still imported as a library and still the backstop for legacy flows).
DoD: SC-1..SC-8 all check out via their §1.2 verification commands.

---

## 12. File-by-file build assignment (5 parallel owners, disjoint files)

**Ground rules:** no owner writes outside their file list. Shared contracts are THIS section — code against it, never against another builder's WIP. All paths relative to `the repo root`. Owner A additionally owns repo scaffolding: `.gitignore`, `.env.example`, `pyproject.toml` (minimal, `[project]` + pytest config only), `tests/conftest.py`, `tests/fixtures/prospects/*.json`. Owner C owns `tests/fixtures/messages/*.json`. Owner D owns `tests/fakes.py` (Owner E imports it read-only).

### Owner A — Core & Data
Files: `src/dm_engine/__init__.py`, `src/dm_engine/config.py`, `src/dm_engine/db.py`, `src/dm_engine/locks.py`, `data/migrations/001_init.sql`, `data/migrations/002_triggers.sql`, `data/migrations/003_views.sql`, `tests/test_db.py`, `tests/test_triggers.py`, `tests/test_locks.py`, `tests/conftest.py`, `.gitignore`, `.env.example`, `pyproject.toml`, `tests/fixtures/prospects/*.json`.
SQL is transcribed verbatim from §3. Dependencies: none.

### Owner B — Prospects, Variants & Stats
Files: `src/dm_engine/prospects.py`, `src/dm_engine/suppression.py`, `src/dm_engine/variants.py`, `src/dm_engine/stats.py`, `data/variants/EXP-2026-08-skeptic.json`, `tests/test_prospects.py`, `tests/test_suppression.py`, `tests/test_variants.py`, `tests/test_stats.py`.
Depends on: A's §12.1–12.2 interfaces.

### Owner C — Compose & Lint
Files: `src/dm_engine/compose.py`, `src/dm_engine/lint.py`, `tests/test_lint.py`, `tests/test_compose.py`, `tests/fixtures/messages/*.json`.
Depends on: A (db/config), B's `variants.get_assignment` (read-only query it may reimplement as SQL — no import needed).

### Owner D — Channels, State & Approval
Files: `src/dm_engine/state.py`, `src/dm_engine/approval.py`, `src/dm_engine/gmail_compat.py`, `src/dm_engine/channels/__init__.py`, `src/dm_engine/channels/base.py`, `src/dm_engine/channels/email_gmail.py`, `src/dm_engine/channels/linkedin_manual.py`, `tests/test_state.py`, `tests/test_approval.py`, `tests/test_channels.py`, `tests/fakes.py`.
Depends on: A (db/config/locks).

### Owner E — Measurement, Reporting & CLI
Files: `src/dm_engine/cli.py`, `src/dm_engine/replies.py`, `src/dm_engine/attribution.py`, `src/dm_engine/report.py`, `tests/test_replies.py`, `tests/test_attribution.py`, `tests/test_report.py`, `tests/test_cli.py`, `tests/adversarial/test_gates.py`.
Depends on: everyone's public interfaces below. `cli.py` is thin dispatch — all logic lives in the owning modules; if a needed function is missing, E stubs a `NotImplementedError` call-through matching the signature here (integration in Phase 6 links them).

### Cross-module contracts (complete — builders code against these verbatim)

**12.1 `config.py` [A]**
```python
@dataclass(frozen=True)
class Config:
    root: Path                 # repo root
    db_path: Path              # default root/'data/dm.db'; env DM_ENGINE_DB overrides
    migrations_dir: Path       # root/'data/migrations'
    variants_dir: Path         # root/'data/variants'
    review_dir: Path           # root/'data/review'
    outbox_dir: Path           # root/'data/outbox'
    locks_dir: Path            # root/'data/locks'
    gmail_skill_root: Path     # Path.home()/'.config/skills/gmail-helper'
    suppression_file: Path     # gmail_skill_root/'suppression_list.txt'
    dashboard_metrics_dir: Path   # Path.home()/'work/dashboard/data/metrics'
    dashboard_approvals_dir: Path # Path.home()/'work/dashboard/data/approvals'
    pending_actions: Path      # Path.home()/'.config/pending-actions.md'
    test_mode: bool            # env DM_ENGINE_TEST == '1'

def load_config() -> Config   # creates data subdirs if missing; in test_mode,
                              # db_path MUST be under a tmp/repo path or raise
```

**12.2 `db.py` / `locks.py` [A]**
```python
def connect(cfg: Config) -> sqlite3.Connection   # Row factory, FK ON, WAL
def migrate(conn, migrations_dir: Path) -> list[int]   # returns versions applied
def phx_date(now: datetime | None = None) -> str        # 'YYYY-MM-DD' at fixed UTC-7
# locks.py
@contextmanager
def hold(cfg: Config, name: str, timeout_s: float = 30.0): ...  # flock; TimeoutError on fail
```

**12.3 `prospects.py` / `suppression.py` / `variants.py` / `stats.py` [B]**
```python
@dataclass class ImportResult: added:int; dupes:int; skips:list[tuple[str,str]]; errors:list[tuple[str,str]]
def import_prospects(conn, path: Path, market: str) -> ImportResult
def normalize_domain(url_or_domain: str) -> str | None
def prospect_key(company_name: str, domain: str | None, market: str) -> str
def qualify(conn, prospect_id: int, actor: str) -> None      # NEW→QUALIFIED
def exclude(conn, prospect_id: int, reason: str, actor: str) -> None
def us_state_tz(us_state: str) -> str                        # raises KeyError on unknown
# suppression.py
def is_suppressed(conn, channel: str, address: str, person_key: str | None = None) -> str | None
def add(conn, channel: str, address: str, scope: str, reason: str, source: str) -> None
def sync_from_file(conn, path: Path) -> int                  # import; returns new rows
def append_to_file(path: Path, address: str, reason: str) -> None   # under '# dm-engine' marker
# variants.py
def load_seed(conn, json_path: Path) -> int                  # idempotent upsert of experiment+variants
def assign_batch(conn, experiment_id: str, batch_id: str) -> list[dict]  # §4.3 algo; abort on drift
def retire(conn, variant_id: str, force: bool, reason: str | None, actor: str) -> None  # gate §4.4
# stats.py
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]
@dataclass class VariantStats: variant_id:str; sends:int; delivered:int; bounced:int; replies:int; booked:int; reply_rate:float|None; booking_rate:float|None; ci:tuple[float,float]|None
def variant_table(conn, experiment_id: str, by_family: bool = False) -> list[VariantStats]
def replies_by_variant(conn, experiment_id: str) -> dict[str, list[dict]]   # verbatim snippets
```

**12.4 `lint.py` / `compose.py` [C]**
```python
@dataclass class MessageDraft: prospect_key:str; channel:str; sequence_step:int; subject:str|None; body:str
@dataclass class LintReport: ok:bool; violations:list[dict]   # {'rule':'L1','detail':...}; json via .to_json()
def normalize(text: str) -> str                                # NFKC + zero-width strip
def lint_message(draft: MessageDraft, batch_subjects: list[str], recent_subjects: list[str],
                 company_casual: str) -> LintReport
def emit_compose_job(conn, cfg, batch_id: str) -> Path         # writes review_dir/<batch>-composejob.json
def ingest_drafts(conn, cfg, path: Path) -> "IngestResult"     # lint each; COMPOSED→READY|LINT_FAILED
@dataclass class IngestResult: ready:int; failed:int; missing:list[str]; unknown:list[str]
```

**12.5 `state.py` / `approval.py` / `channels/*` / `gmail_compat.py` [D]**
```python
class IllegalTransition(Exception): ...
def transition(conn, entity: str, entity_id, to_state: str, actor: str,
               reason: str | None = None, evidence: str | None = None) -> None
# approval.py
def write_review_file(conn, cfg, batch_id: str) -> Path
@dataclass class ApproveResult: approved:int; rejected:int; skipped:int
def approve_from_review(conn, cfg, review_path: Path, actor: str = 'operator') -> ApproveResult
def approve_interactive(conn, cfg, batch_id: str) -> ApproveResult   # TTY only; else raise NotATTY
# channels/base.py
@dataclass class ChannelConstraints: max_words:int|None; max_sentences:int|None; max_chars:int|None; has_subject:bool
@dataclass class StageResult: staged:int; skipped:list[tuple[int,str]]; failed:list[tuple[int,str]]
class ChannelAdapter(Protocol):
    name: str
    def constraints(self) -> ChannelConstraints: ...
    def stage(self, conn, cfg, message_ids: list[int], dry_run: bool,
              service_factory=None) -> StageResult: ...      # APPROVED rows only; cap+lock inside
    def reconcile(self, conn, cfg, service_factory=None) -> dict: ...  # {'sent':n,'bounced':n}
def get_adapter(channel: str) -> ChannelAdapter
# gmail_compat.py — the ONLY module that touches the gmail-helper skill
SKILL_SCRIPTS = Path.home()/'.config/skills/gmail-helper/scripts'   # sys.path.insert once
def get_service(cfg, account: str = 'operator_personal')      # raises TestModeError if cfg.test_mode
def verify_batch_smtp(cfg, emails: list[str]) -> list[dict] # wraps verify_emails.verify_batch(smtp=True)
def create_draft(service, to, subject, body) -> str          # passthrough to create_drafts.create_draft
def already_sent_to(service, email) -> bool                  # passthrough
def has_inbound_reply(service, email) -> bool                # passthrough (fail-closed)
DEMO_LINK: str                                               # re-export from create_drafts
```

**12.6 `replies.py` / `attribution.py` / `report.py` / `cli.py` [E]**
```python
def scan(conn, cfg, service_factory=None) -> dict            # {'new_replies':n,'bounces':n,'auto':n}
def ingest_triage(conn, cfg, path: Path) -> int
def record_reply(conn, prospect_id:int, channel:str, classification:str, snippet:str, actor:str) -> None
# attribution.py
@dataclass class MatchCandidate: prospect_id:int; ghl_contact_id:str; method:str; confidence:str; evidence:str
def match(conn, ghl_export_path: Path) -> list[MatchCandidate]   # auto-writes exact/high; cards for manual
def book(conn, prospect_id:int, method:str, evidence:str, actor:str) -> None
# report.py
def sheet_rows(conn, full: bool = False) -> list[dict]
def write_csv(rows, path: Path) -> None
def emit_dashboard_metrics(conn, cfg) -> Path
def write_approvals_card(cfg, card_id: str, title: str, summary: str, link: str) -> Path
```
`cli.py` subcommands (argparse, exit codes: 0 ok, 1 partial, 2 refused/gate, 3 gate-abort from create_drafts precedent, 4 auth): `init, import-prospects, qualify, exclude, set-casual, verify, new-batch, assign, compose, ingest-drafts, review, approve, stage, reconcile, mark-sent, scan-replies, ingest-triage, record-reply, attrib, book, sheet, stats, metrics-emit, suppress, status, cancel, drop-from-batch`.

**12.7 Prospect import file contract** (produced by lead-scout/gmaps-leads agent steps):
```json
[{"company_name":"Acme Hardscape","website_url":"https://acmehardscapeaz.com",
  "us_state":"AZ","owner_first_name":"Alex","icp_fit_note":"8-man crew, owner still installing",
  "hook_fact":"212 five-star reviews on Google","company_casual":"acme hardscape",
  "emails":[{"address":"alex@acmehardscapeaz.com","source":"website"}],
  "linkedin":[{"address":"https://linkedin.com/in/alex-x","source":"linkedin"}],
  "source":"lead-scout","source_detail":"LEA-batch prompt 2026-08"}]
```

**12.8 Draft ingest file contract** (produced by outreach-specialist from the compose job):
```json
{"batch_id":"B-20260803-boise-1",
 "drafts":[{"prospect_key":"acmehardscapeaz.com","channel":"email","sequence_step":1,
            "subject":"the compactor doesn't take messages","body":"..."}]}
```

---

## 13. Test strategy

- **Unit (pure, no I/O):** lint rules, normalization, wilson, domain/name normalization, phx_date, assignment algo, state-transition table. Command: `pytest tests/test_lint.py tests/test_stats.py tests/test_variants.py -q`.
- **DB-backed (tmp SQLite per test):** `conftest.py` fixture `engine_db` builds a fresh migrated DB in `tmp_path`; all trigger tests fire raw SQL and assert aborts. `pytest tests/test_db.py tests/test_triggers.py -q`.
- **Fakes (Owner D, `tests/fakes.py`):** `FakeGmailService` implementing exactly the call shapes used (`users().drafts().create/list/delete`, `users().messages().list/get`) over in-memory dicts, with a seedable `sent` store and failure injection (`fail_on_call=n` for partial-batch tests). `FakeServiceFactory` passed as `service_factory=`.
- **Real-data guarantee — how "no test touches real Gmail/GHL" is mechanical, not aspirational:** `conftest.py` sets `DM_ENGINE_TEST=1` for the whole session; under it `gmail_compat.get_service()` raises `TestModeError`, `config.load_config()` refuses a db_path outside tmp/repo, `report.emit_dashboard_metrics` and `write_approvals_card` write into `tmp_path`-overridden dirs (Config built by the fixture), and an import-hook assertion fails any test that imports `googleapiclient`. GHL is only ever a disk file in this system, so tests use fixture exports.
- **Dry-run verified (human, Phase 4/7):** the single-draft real smoke to the operator's own address; `dmctl stage --dry-run` inspection of outbox JSON; `verify_emails.py --smtp` live run against the operator's own address from the VPS.
- **Adversarial suite:** `pytest tests/adversarial/test_gates.py -q` — the §10 table, one test per row.
- **Full gate:** `pytest -q` green is the merge bar for every phase.

---

## 14. Explicit non-goals (do not build these)

1. **No sending code, ever** — not even behind a flag. Sends live in EXEC/`send_*.py`/human hands.
2. No LinkedIn/Instagram automated DMs or connection requests. Research reads only.
3. No cold SMS (TCPA). No `sms` adapter.
4. No web UI / dashboard — DASHBOARD is the surface (metrics JSON + approvals cards).
5. No scheduler/daemon inside the engine — EXEC send-queue and cron already exist.
6. No ORM, no Pydantic, no third-party deps beyond what's already installed (googleapiclient + dnspython via the skill scripts).
7. No email-infrastructure work (warm-up, new domains, DKIM changes) — operatorchua.aj as-is.
8. No GHL writes of any kind. Read-only via disk exports.
9. No revenue/pipeline-value math (GHL monetaryValue is all zeros — garbage in).
10. No multi-user/auth, no VPS↔WSL DB sync, no historical stats backfill (history informs suppression/dedup only, via Gmail Sent checks).
11. No statistical machinery beyond §4.4 (no Bayesian bandits, no auto-optimization — assignment stays deterministic and dumb until n justifies more).
12. No paid data sources; Apollo free tier only, and only as a RISK-class address source.

---

## 15. Open questions for the operator (each with a build-safe default — the build proceeds on defaults)

| # | Question | Recommended default (build assumes this) |
|---|---|---|
| Q1 | Confirm GHL demo form captures UTM params into contact attribution (test with one manual submission carrying `?utm_content=test-key`). | Ship UTM params on the Message-2 link regardless — harmless if dropped; attribution falls back to domain/name/manual (§8.2). |
| Q2 | Keep the engine-level approval step for EMAIL (double review: review-file + Gmail), or review in Gmail only? | Keep it. It is one `approve-all: yes` edit per batch, it is the only human check LinkedIn gets, and it is what makes G3 provable. |
| Q3 | Add Instagram DM as a third channel now? | Defer. Schema-ready; clone `linkedin_manual.py` when wanted. |
| Q4 | Pilot pacing to hit the report's 20 attempts? | Two batches of 10 (unique-body class, ≤30/day cap) on consecutive weekdays, EXEC-queued at prospect-local late morning. |
| Q5 | browser-helper LinkedIn research caps? | ≤25 profile views/day, ≤3 sessions, hard-stop on checkpoint (§6.2). |
| Q6 | Where does the dm-engine git repo live? | `git init` inside `the repo root` (deploy-automation precedent: BDAI root stays non-git, subprojects are repos). `.gitignore` lands in the first commit, before any `.env`. |
| Q7 | Cron the read-only `reconcile` + `scan-replies`? | Not in v1; run them when checking the pipeline (`dmctl status` prints reminders if last scan >48h). Add a VPS cron later if it nags. |
| Q8 | Variant min-n gate value? | 30 delivered per variant + ≥5 total human replies (§4.4). Adjustable per variant via `min_n_gate` column. |

---

*End of plan. Builders: read §12 twice, then build only your files.*
