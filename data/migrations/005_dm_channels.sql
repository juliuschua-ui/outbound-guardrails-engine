-- 005_dm_channels.sql — WhatsApp and Facebook Messenger as first-class DM channels.
--
-- the operator's real intent is DM-first outreach; email is the secondary manual channel.
-- Neither of the two new channels has any send path in this engine and never will:
-- Meta's Business APIs prohibit cold business-initiated messaging (WhatsApp needs
-- opt-in plus a pre-approved template, Messenger has a 24-hour user-initiated
-- window), so the engine stages a copy-ready card and the operator sends it by hand from
-- his own account, exactly as a person using the app. See
-- src/dm_engine/channels/whatsapp_manual.py and messenger_manual.py — they contain
-- no HTTP client, no browser driver, no subprocess, not behind a flag.
--
-- Four CHECK constraints have to widen, and SQLite cannot ALTER a CHECK. Each
-- affected table is therefore rebuilt.
--
--   contacts.channel     ('email','linkedin','instagram','sms')            + 2
--   suppression.channel  ('email','linkedin','instagram','sms','all')      + 2
--   batches.channel      ('email','linkedin')                              + 2
--   messages.channel     ('email','linkedin')                              + 2
--
-- ── how the rebuild is done, and why not the textbook 12 steps ─────────────────
--
-- The textbook procedure opens with `PRAGMA foreign_keys=off` and closes with a
-- rename. Neither works here:
--
--   * `foreign_keys` is a NO-OP inside a transaction, and db.migrate wraps every
--     migration in `BEGIN; … COMMIT;`. `defer_foreign_keys` is the pragma that
--     does work inside a transaction, so that is the one used.
--   * `ALTER TABLE … RENAME TO` re-parses every view and trigger in the schema. A
--     view or trigger naming a table that is mid-rebuild makes the rename fail
--     outright ("error in view sheet: no such table"). Renaming also rewrites the
--     stored SQL as `CREATE TABLE "contacts"`, quoted, which is schema churn for
--     no gain.
--   * Under deferred FKs, DROP TABLE's implicit DELETE of a parent row counts one
--     violation per orphaned child, and re-inserting the parent into a DIFFERENT
--     table (the `_new` table, before the rename) never clears that count — the
--     commit then fails. Inserting the rows back into a table that already carries
--     the final name DOES clear it, because SQLite rescans the children.
--
-- So each table is rebuilt as: stash the rows in a plain scratch table → DROP the
-- real table → CREATE it again under its own name with the widened CHECK → copy
-- the rows back → drop the scratch. No rename, no quoting artefacts, and the
-- resulting `sqlite_master.sql` is exactly the text below.
--
-- Everything attached to a rebuilt table is dropped with it and is recreated here
-- verbatim: 11 triggers on `messages` (G1–G9, G11, G12 — the security core), the
-- three explicit indexes on `contacts`, the three on `messages`, and the `sheet`
-- view. The UNIQUE/PRIMARY KEY auto-indexes come back with the table definitions.
-- `tests/test_db.py::test_incremental_migration_matches_fresh_install` is the
-- proof: it compares the whole of sqlite_master between a two-pass migration and a
-- fresh install, so anything dropped and not restored fails it.

PRAGMA defer_foreign_keys = ON;

-- --------------------------------------------------------------------------- --
-- step 0: detach everything that names a table we are about to rebuild
-- --------------------------------------------------------------------------- --
-- The `sheet` view reads messages + contacts; all 11 message triggers read
-- messages, and two of them read contacts. Left in place, the first DROP TABLE
-- leaves the schema unparseable for the rest of the script.

DROP VIEW sheet;

DROP TRIGGER trg_msg_born_composed;
DROP TRIGGER trg_msg_approve_path;
DROP TRIGGER trg_msg_stage_path;
DROP TRIGGER trg_msg_sent_path;
DROP TRIGGER trg_msg_ready_lint;
DROP TRIGGER trg_msg1_no_pitch;
DROP TRIGGER trg_msg2_reply_gate;
DROP TRIGGER trg_msg_suppressed;
DROP TRIGGER trg_email_verify_gate;
DROP TRIGGER trg_one_msg1_per_prospect;
DROP TRIGGER trg_msg_prospect_suppressed;

-- --------------------------------------------------------------------------- --
-- contacts: widened channel CHECK + channel-aware canonical address
-- --------------------------------------------------------------------------- --
-- Migration 004 made `address_canon` fold '+tag' and gmail dots so that suppressing
-- one spelling of a mailbox blocks the others. A phone number has the same problem
-- and a worse blast radius: `+1 (602) 555-0134`, `602-555-0134` and `16025550134`
-- are one human, and a hard-no that misses on punctuation means messaging someone
-- who told the operator to stop. So `address_canon` becomes channel-aware:
--
--   whatsapp   -> digits only, US country code dropped (matches
--                 attribution.normalize_phone and suppression.canonical_phone)
--   messenger  -> the page/profile handle, host and scheme stripped (matches
--                 suppression.canonical_messenger)
--   email      -> unchanged from 004
--
-- The two helper columns exist because SQLite has no regex: the punctuation strip
-- has to be a fixed chain of replace() calls, and referencing it twice inline would
-- mean writing that chain twice. Both are kept in lockstep with the Python
-- canonicalizers by tests/test_suppression.py, which asserts the SQL and the Python
-- agree on the same table of inputs.

CREATE TABLE contacts_stash AS
  SELECT id, prospect_id, person_key, channel, address, address_source,
         verify_verdict, verify_reason, verified_at, created_at
  FROM contacts;

DROP TABLE contacts;

CREATE TABLE contacts (
  id              INTEGER PRIMARY KEY,
  prospect_id     INTEGER NOT NULL REFERENCES prospects(id),
  person_key      TEXT,                    -- normalized email / linkedin slug / phone
                                           -- digits / messenger handle; detects the
                                           -- same human across companies
  channel         TEXT NOT NULL CHECK (channel IN
                    ('email','linkedin','instagram','sms','whatsapp','messenger')),
  address         TEXT NOT NULL,           -- email addr / profile URL / phone / page URL
  address_source  TEXT NOT NULL CHECK (address_source IN
                    ('website','reply','linkedin','directory','apollo','manual','format_guess')),
  verify_verdict  TEXT CHECK (verify_verdict IN ('PASS','RISK','FAIL')),
  verify_reason   TEXT,
  verified_at     TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),

  addr_lc         TEXT GENERATED ALWAYS AS (lower(trim(address))) VIRTUAL,

  -- addr_lc with any '#fragment' and '?query' cut off. A profile URL copied out of
  -- a browser carries a trailing '#', and a scraper hands back '?ref=...'; without
  -- this they canonicalize to a DIFFERENT identity than the bare URL, so a hard-no
  -- recorded against one spelling silently fails to block the other. Same failure
  -- class as an unfolded '+tag' on an email.
  -- `x || delim` guarantees instr() finds one, which removes the need for a CASE.
  -- Truncating before the scheme strip is safe: no scheme contains '#' or '?'.
  addr_nofrag     TEXT GENERATED ALWAYS AS (
                    substr(
                      substr(addr_lc, 1, instr(addr_lc || '#', '#') - 1), 1,
                      instr(substr(addr_lc, 1, instr(addr_lc || '#', '#') - 1) || '?',
                            '?') - 1)) VIRTUAL,

  addr_local      TEXT GENERATED ALWAYS AS (
                    CASE WHEN instr(addr_lc, '@') = 0 THEN addr_lc
                         ELSE substr(addr_lc, 1, instr(addr_lc, '@') - 1) END) VIRTUAL,

  -- googlemail.com is an alias of gmail.com, not a second provider.
  addr_host       TEXT GENERATED ALWAYS AS (
                    CASE WHEN instr(addr_lc, '@') = 0 THEN ''
                         WHEN substr(addr_lc, instr(addr_lc, '@') + 1) = 'googlemail.com'
                              THEN 'gmail.com'
                         ELSE substr(addr_lc, instr(addr_lc, '@') + 1) END) VIRTUAL,

  -- the punctuation a phone number is written with, removed
  addr_stripped   TEXT GENERATED ALWAYS AS (
                    replace(replace(replace(replace(replace(replace(replace(
                      addr_lc, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''),
                      '.', ''), '/', '')) VIRTUAL,

  -- E.164 digits with the US country code dropped, so one number has one identity.
  -- Anything that is not a phone number at all falls back to addr_lc untouched: the
  -- whatsapp channel also carries person keys, and stripping the punctuation out of
  -- one ('alex-x' -> 'alexx') would stop a person-scope row matching it.
  addr_phone      TEXT GENERATED ALWAYS AS (
                    CASE WHEN addr_stripped = '' OR addr_stripped GLOB '*[^0-9]*'
                         THEN addr_lc
                         WHEN length(addr_stripped) = 11 AND substr(addr_stripped, 1, 1) = '1'
                         THEN substr(addr_stripped, 2)
                         ELSE addr_stripped END) VIRTUAL,

  -- m.me / facebook.com / messenger.com all address the same Page; the handle is
  -- what identifies it. The trailing-slash trim happens BEFORE the 't/' strip on
  -- purpose: a page called '.../pinelandscapemgmt/' would otherwise lose its
  -- last two characters. Ordered exactly like suppression.canonical_messenger.
  addr_handle     TEXT GENERATED ALWAYS AS (
                    replace(replace(replace(replace(replace(replace(replace(
                      rtrim(replace(replace(replace(addr_nofrag,
                        'https://', ''), 'http://', ''), 'www.', ''), '/'),
                      'm.me/', ''), 'fb.me/', ''), 'fb.com/', ''),
                      'messenger.com/', ''), 'facebook.com/', ''),
                      'messages/', ''), 't/', '')) VIRTUAL,

  -- '+tag' is stripped for every provider (a literal '+' in a real mailbox is
  -- vanishingly rare, and this is a fail-closed list). Dots are folded ONLY for
  -- gmail.com, where it is a documented provider rule rather than a guess.
  address_canon   TEXT GENERATED ALWAYS AS (
                    CASE
                      WHEN channel = 'whatsapp'  THEN addr_phone
                      WHEN channel = 'messenger' THEN addr_handle
                      WHEN instr(addr_lc, '@') = 0 THEN addr_lc
                      ELSE
                        (CASE WHEN addr_host = 'gmail.com'
                              THEN replace(CASE WHEN instr(addr_local, '+') > 0
                                                THEN substr(addr_local, 1, instr(addr_local, '+') - 1)
                                                ELSE addr_local END, '.', '')
                              ELSE      CASE WHEN instr(addr_local, '+') > 0
                                                THEN substr(addr_local, 1, instr(addr_local, '+') - 1)
                                                ELSE addr_local END
                         END) || '@' || addr_host
                    END) VIRTUAL,

  UNIQUE (channel, address)
);

INSERT INTO contacts (id, prospect_id, person_key, channel, address, address_source,
                      verify_verdict, verify_reason, verified_at, created_at)
  SELECT id, prospect_id, person_key, channel, address, address_source,
         verify_verdict, verify_reason, verified_at, created_at
  FROM contacts_stash;

DROP TABLE contacts_stash;

CREATE INDEX idx_contacts_person ON contacts(person_key);
CREATE INDEX idx_contacts_prospect ON contacts(prospect_id);
CREATE INDEX idx_contacts_canon ON contacts(address_canon);

-- --------------------------------------------------------------------------- --
-- suppression: the two new channels can carry a do-not-contact row
-- --------------------------------------------------------------------------- --

CREATE TABLE suppression_stash AS
  SELECT id, channel, address, scope, reason, source, created_at FROM suppression;

DROP TABLE suppression;

CREATE TABLE suppression (
  id          INTEGER PRIMARY KEY,
  channel     TEXT NOT NULL CHECK (channel IN
               ('email','linkedin','instagram','sms','whatsapp','messenger','all')),
  address     TEXT NOT NULL,               -- lowercase email / profile URL / phone digits /
                                           -- messenger handle / '@domain' for scope=domain
  scope       TEXT NOT NULL DEFAULT 'address' CHECK (scope IN ('address','domain','person')),
  reason      TEXT NOT NULL CHECK (reason IN
               ('hard_no','unsubscribe','bounce','dead_mailbox','legal','manual')),
  source      TEXT,                        -- 'file_sync'|'reply_scan'|'dmctl'|...
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (channel, address)
);

INSERT INTO suppression (id, channel, address, scope, reason, source, created_at)
  SELECT id, channel, address, scope, reason, source, created_at FROM suppression_stash;

DROP TABLE suppression_stash;

-- --------------------------------------------------------------------------- --
-- batches
-- --------------------------------------------------------------------------- --

CREATE TABLE batches_stash AS
  SELECT id, experiment_id, market, channel, personalization, batch_seq, state,
         notes, created_at
  FROM batches;

DROP TABLE batches;

CREATE TABLE batches (
  id               TEXT PRIMARY KEY,       -- 'B-20260803-boise-1'
  experiment_id    TEXT NOT NULL REFERENCES experiments(id),
  market           TEXT NOT NULL,
  channel          TEXT NOT NULL CHECK (channel IN
                     ('email','linkedin','whatsapp','messenger')),
  personalization  TEXT NOT NULL DEFAULT 'unique'
                   CHECK (personalization IN ('template','unique')),
  batch_seq        INTEGER NOT NULL,       -- monotonically increasing per experiment; used
                                           -- for cross-batch round-robin offset (§4.3)
  state            TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN
                    ('OPEN','COMPOSED','REVIEWED','STAGED','CLOSED','ABORTED')),
  notes            TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO batches (id, experiment_id, market, channel, personalization, batch_seq,
                     state, notes, created_at)
  SELECT id, experiment_id, market, channel, personalization, batch_seq, state,
         notes, created_at
  FROM batches_stash;

DROP TABLE batches_stash;

-- --------------------------------------------------------------------------- --
-- messages
-- --------------------------------------------------------------------------- --

CREATE TABLE messages_stash AS
  SELECT id, batch_id, prospect_id, contact_id, assignment_id, variant_id,
         sequence_step, channel, subject, body, state, lint_report, approved_at,
         approved_via, gmail_draft_id, gmail_message_id, gmail_thread_id, staged_at,
         sent_at, sent_evidence, created_at, updated_at
  FROM messages;

DROP TABLE messages;

CREATE TABLE messages (
  id             INTEGER PRIMARY KEY,
  batch_id       TEXT NOT NULL REFERENCES batches(id),
  prospect_id    INTEGER NOT NULL REFERENCES prospects(id),
  contact_id     INTEGER NOT NULL REFERENCES contacts(id),
  assignment_id  INTEGER NOT NULL REFERENCES assignments(id),
  variant_id     TEXT NOT NULL REFERENCES variants(id),   -- denormalized for the sheet query
  sequence_step  INTEGER NOT NULL DEFAULT 1 CHECK (sequence_step IN (1,2)),
  channel        TEXT NOT NULL CHECK (channel IN
                  ('email','linkedin','whatsapp','messenger')),
  subject        TEXT,                     -- email only; NULL on every DM channel
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

INSERT INTO messages (id, batch_id, prospect_id, contact_id, assignment_id, variant_id,
                      sequence_step, channel, subject, body, state, lint_report,
                      approved_at, approved_via, gmail_draft_id, gmail_message_id,
                      gmail_thread_id, staged_at, sent_at, sent_evidence, created_at,
                      updated_at)
  SELECT id, batch_id, prospect_id, contact_id, assignment_id, variant_id,
         sequence_step, channel, subject, body, state, lint_report, approved_at,
         approved_via, gmail_draft_id, gmail_message_id, gmail_thread_id, staged_at,
         sent_at, sent_evidence, created_at, updated_at
  FROM messages_stash;

DROP TABLE messages_stash;

CREATE INDEX idx_messages_state ON messages(state);
CREATE INDEX idx_messages_prospect ON messages(prospect_id);
CREATE INDEX idx_messages_batch ON messages(batch_id);

-- --------------------------------------------------------------------------- --
-- the gates, back exactly as they were (002_triggers.sql, 004 for G8 and G12)
-- --------------------------------------------------------------------------- --
-- Only G8 changes, and only to widen: see the note above it. Every other trigger
-- below is the text from 002/004, character for character.

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

-- G5: READY requires a lint_report whose "ok" is true. `IS NOT 1` is the null-safe
-- form: `!= 1` yields NULL (does not fire) for a report with no "ok" key.
CREATE TRIGGER trg_msg_ready_lint BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'READY'
 AND (NEW.lint_report IS NULL OR json_extract(NEW.lint_report, '$.ok') IS NOT 1)
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

-- G8, widened twice against the 004 version:
--
--   1. a `person`-scope row now matches on ANY channel. A person-scope suppression
--      is a fact about a HUMAN, not about one app: the whole point of the DM build
--      is that the operator reaches the same owner on WhatsApp, Messenger and email, so a
--      hard-no taken on one of them that left the other two open would be a hole
--      the operator would never guess was there. `address` and `domain` scope stay
--      channel-scoped, which is what keeps a dead mailbox from muting a Page.
--   2. `domain` scope can no longer match a whatsapp or messenger contact. There is
--      no domain in a phone number or a page handle, and the fallback branch
--      (`addr_host = ''` -> suffix LIKE) would otherwise let a short domain row
--      match by coincidence on the tail of a phone number.
CREATE TRIGGER trg_msg_suppressed BEFORE INSERT ON messages
WHEN EXISTS (
  SELECT 1 FROM contacts c
  JOIN suppression s
    ON (s.channel = c.channel OR s.channel = 'all' OR s.scope = 'person')
  WHERE c.id = NEW.contact_id
    AND ( (s.scope = 'address'
           AND (s.channel = c.channel OR s.channel = 'all')
           AND (lower(s.address) = c.addr_lc OR lower(s.address) = c.address_canon))
       OR (s.scope = 'person'
           AND (lower(s.address) = lower(c.person_key)
                OR lower(s.address) = c.address_canon))
       OR (s.scope = 'domain'
           AND (s.channel = c.channel OR s.channel = 'all')
           AND c.channel NOT IN ('whatsapp','messenger')
           AND ltrim(lower(s.address), '@') <> ''
           AND (   c.addr_host = ltrim(lower(s.address), '@')
                OR c.addr_host LIKE '%.' || ltrim(lower(s.address), '@')
                OR (c.addr_host = '' AND c.addr_lc LIKE '%' || lower(s.address)) )) ))
BEGIN SELECT RAISE(ABORT, 'G8: contact is on the suppression list'); END;

-- G9: email staging requires PASS, or RISK from a verified (non-guessed) source.
CREATE TRIGGER trg_email_verify_gate BEFORE UPDATE OF state ON messages
WHEN NEW.state = 'STAGED' AND NEW.channel = 'email'
 AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = NEW.contact_id
     AND ( c.verify_verdict = 'PASS'
        OR (c.verify_verdict = 'RISK'
            AND c.address_source IN ('website','reply','linkedin','manual')) ))
BEGIN SELECT RAISE(ABORT, 'G9: email not PASS-verified (or RISK from format guess)'); END;

-- G11: a second Message-1 to the same prospect is forbidden (dup-touch guard).
CREATE TRIGGER trg_one_msg1_per_prospect BEFORE INSERT ON messages
WHEN NEW.sequence_step = 1 AND EXISTS (
  SELECT 1 FROM messages m WHERE m.prospect_id = NEW.prospect_id
    AND m.sequence_step = 1
    AND m.state NOT IN ('LINT_FAILED','REJECTED','CANCELLED'))
BEGIN SELECT RAISE(ABORT, 'G11: prospect already has an active Message 1'); END;

-- G12: a SUPPRESSED prospect can never receive another message.
CREATE TRIGGER trg_msg_prospect_suppressed BEFORE INSERT ON messages
WHEN EXISTS (SELECT 1 FROM prospects p
             WHERE p.id = NEW.prospect_id AND p.state = 'SUPPRESSED')
BEGIN SELECT RAISE(ABORT, 'G12: prospect is SUPPRESSED — the company asked to stop'); END;

-- --------------------------------------------------------------------------- --
-- the sheet view, back verbatim from 003_views.sql
-- --------------------------------------------------------------------------- --

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
