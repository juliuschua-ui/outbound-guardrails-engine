-- 004_suppression_binding.sql — close the "suppression can be walked around" class.
--
-- Three holes, one root: the engine compared addresses as opaque strings and had
-- no notion of a suppressed COMPANY.
--
--   1. plus-addressing and Gmail dot-folding walked past G8. `alex@acme.com`
--      suppressed did not stop `alex+quote@acme.com`, and for a gmail.com
--      mailbox `a.lex@gmail.com` is the SAME human, deterministically, per
--      Google's own documented rule. contacts.address_canon folds both.
--   2. a domain-scope row only matched an exact host suffix, so a suppressed
--      `@acme.com` missed `alex@mail.acme.com`, and (worse) a row stored
--      without the leading '@' matched `x@notacme.com` by plain string suffix.
--   3. nothing in the engine ever wrote prospects.state = 'SUPPRESSED', so a
--      company that unsubscribed could be re-imported under a second market key
--      (a name-slug twin) and receive a fresh Message 1 — G11 is keyed on
--      prospect_id, and the twin is a different row. G12 below closes that.
--
-- The canonical form is deliberately NOT stored on the row we send to: the
-- address we email stays exactly the address a human saw. address_canon is a
-- VIRTUAL generated column used only for matching.

-- --------------------------------------------------------------------------- --
-- canonical address (matching only — never the address we send to)
-- --------------------------------------------------------------------------- --

ALTER TABLE contacts ADD COLUMN addr_lc TEXT
  GENERATED ALWAYS AS (lower(trim(address))) VIRTUAL;

ALTER TABLE contacts ADD COLUMN addr_local TEXT
  GENERATED ALWAYS AS (
    CASE WHEN instr(addr_lc, '@') = 0 THEN addr_lc
         ELSE substr(addr_lc, 1, instr(addr_lc, '@') - 1) END) VIRTUAL;

-- googlemail.com is an alias of gmail.com, not a second provider.
ALTER TABLE contacts ADD COLUMN addr_host TEXT
  GENERATED ALWAYS AS (
    CASE WHEN instr(addr_lc, '@') = 0 THEN ''
         WHEN substr(addr_lc, instr(addr_lc, '@') + 1) = 'googlemail.com' THEN 'gmail.com'
         ELSE substr(addr_lc, instr(addr_lc, '@') + 1) END) VIRTUAL;

-- '+tag' is stripped for every provider (a literal '+' in a real mailbox is
-- vanishingly rare, and this is a fail-closed list). Dots are folded ONLY for
-- gmail.com, where it is a documented provider rule rather than a guess.
ALTER TABLE contacts ADD COLUMN address_canon TEXT
  GENERATED ALWAYS AS (
    CASE
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
    END) VIRTUAL;

CREATE INDEX idx_contacts_canon ON contacts(address_canon);

-- --------------------------------------------------------------------------- --
-- G8, rebuilt: canonical + domain-aware
-- --------------------------------------------------------------------------- --

DROP TRIGGER trg_msg_suppressed;

CREATE TRIGGER trg_msg_suppressed BEFORE INSERT ON messages
WHEN EXISTS (
  SELECT 1 FROM contacts c
  JOIN suppression s ON (s.channel = c.channel OR s.channel = 'all')
  WHERE c.id = NEW.contact_id
    AND ( (s.scope = 'address'
           AND (lower(s.address) = c.addr_lc OR lower(s.address) = c.address_canon))
       OR (s.scope = 'person'
           AND (lower(s.address) = lower(c.person_key)
                OR lower(s.address) = c.address_canon))
       OR (s.scope = 'domain'
           AND ltrim(lower(s.address), '@') <> ''
           AND (   c.addr_host = ltrim(lower(s.address), '@')
                OR c.addr_host LIKE '%.' || ltrim(lower(s.address), '@')
                OR (c.addr_host = '' AND c.addr_lc LIKE '%' || lower(s.address)) )) ))
BEGIN SELECT RAISE(ABORT, 'G8: contact is on the suppression list'); END;

-- --------------------------------------------------------------------------- --
-- G12: a SUPPRESSED prospect can never receive another message
-- --------------------------------------------------------------------------- --
-- G11 stops a second Message 1 per prospect ROW. It cannot see that two rows are
-- the same company (the website-less name-slug key is market-scoped). Suppression
-- is recorded on the company, so the gate belongs on the company too.

CREATE TRIGGER trg_msg_prospect_suppressed BEFORE INSERT ON messages
WHEN EXISTS (SELECT 1 FROM prospects p
             WHERE p.id = NEW.prospect_id AND p.state = 'SUPPRESSED')
BEGIN SELECT RAISE(ABORT, 'G12: prospect is SUPPRESSED — the company asked to stop'); END;

-- --------------------------------------------------------------------------- --
-- sync bookkeeping: the shared file must not be able to vanish silently
-- --------------------------------------------------------------------------- --
-- One row, rewritten on every sync. Without it, a suppression_list.txt that was
-- deleted or truncated between runs is indistinguishable from "nobody is
-- suppressed" — which is exactly the failure this whole file exists to prevent.

CREATE TABLE suppression_sync (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  path            TEXT NOT NULL,
  file_addresses  INTEGER NOT NULL,
  status          TEXT NOT NULL,      -- 'ok'|'absent'
  synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
