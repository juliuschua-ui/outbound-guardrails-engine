-- 006_messenger_profile_id.sql — the SQL belt agrees with canonical_messenger again.
--
-- `facebook.com/profile.php?id=<N>` is the one Messenger spelling where the PATH is
-- identical for everybody and the identity lives in the QUERY STRING. `addr_nofrag`
-- cuts the query off before `addr_handle` ever sees it, so EVERY profile.php URL
-- folded onto the literal handle 'profile.php'. One hard-no recorded against any of
-- them therefore suppressed every unrelated prospect whose page is a profile.php URL.
--
-- suppression.canonical_messenger was fixed in Python to read the id; this migration
-- restates the same rule in the generated column so the two belts agree. They are a
-- pair: Python decides at the stage gate, the column backs trigger G8. A disagreement
-- means one of them silently stops matching.
--
-- Only `contacts.addr_handle` changes. SQLite cannot ALTER a generated column, so the
-- table is rebuilt with the SAME procedure 005 documents and for the same reasons:
-- `defer_foreign_keys` (not `foreign_keys`, a no-op inside the transaction db.migrate
-- opens), a plain scratch table rather than ALTER … RENAME (which re-parses every view
-- and trigger), and the rows copied back into a table that already carries the final
-- name so deferred-FK violations from the implicit DELETE are cleared.
--
-- Rows are copied by STORED column only; every generated column recomputes on read, so
-- existing profile.php contacts pick up the corrected identity with no data migration.

PRAGMA defer_foreign_keys = ON;

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
  -- m.me / facebook.com / messenger.com all address the same Page; the handle is
  -- what identifies it. The trailing-slash trim happens BEFORE the 't/' strip on
  -- purpose: a page called '.../pinelandscapemgmt/' would otherwise lose its
  -- last two characters. Ordered exactly like suppression.canonical_messenger.
  --
  -- 006: `profile.php?id=<N>` is the one Messenger spelling where the PATH is the same
  -- for everybody and the identity lives in the QUERY. addr_nofrag cuts the query off,
  -- so the old expression folded every such profile onto the literal handle
  -- 'profile.php' — one hard-no there suppressed every unrelated prospect whose page is
  -- a profile.php URL. The id is read off addr_lc, which keeps the query. A profile.php
  -- URL with no usable id canonicalizes to '' — it names nobody. Matches
  -- suppression.canonical_messenger; the two must not drift.
  --
  -- The substr() that takes everything after 'id=' is repeated rather than aliased:
  -- SQLite prohibits subqueries inside a generated column, so there is no way to name
  -- it once. Unlike the Python side it does not require the id to be digits — a
  -- non-numeric id yields a distinct handle instead of '', which is the safe direction
  -- (it groups nothing that was not already one page).
  addr_handle     TEXT GENERATED ALWAYS AS (
                    CASE
                      WHEN instr(addr_lc, 'profile.php') = 0 THEN (replace(replace(replace(replace(replace(replace(replace(
                      rtrim(replace(replace(replace(addr_nofrag,
                        'https://', ''), 'http://', ''), 'www.', ''), '/'),
                      'm.me/', ''), 'fb.me/', ''), 'fb.com/', ''),
                      'messenger.com/', ''), 'facebook.com/', ''),
                      'messages/', ''), 't/', ''))
                      WHEN instr(addr_lc, '?id=') > 0 OR instr(addr_lc, '&id=') > 0
                        THEN CASE WHEN instr(substr(addr_lc, CASE WHEN instr(addr_lc, '?id=') > 0 THEN instr(addr_lc, '?id=') + 4 ELSE instr(addr_lc, '&id=') + 4 END), '&') > 0
                                  THEN substr(substr(addr_lc, CASE WHEN instr(addr_lc, '?id=') > 0 THEN instr(addr_lc, '?id=') + 4 ELSE instr(addr_lc, '&id=') + 4 END), 1, instr(substr(addr_lc, CASE WHEN instr(addr_lc, '?id=') > 0 THEN instr(addr_lc, '?id=') + 4 ELSE instr(addr_lc, '&id=') + 4 END), '&') - 1)
                                  ELSE substr(addr_lc, CASE WHEN instr(addr_lc, '?id=') > 0 THEN instr(addr_lc, '?id=') + 4 ELSE instr(addr_lc, '&id=') + 4 END) END
                      ELSE ''
                    END) VIRTUAL,

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

CREATE INDEX idx_contacts_person   ON contacts(person_key);
CREATE INDEX idx_contacts_prospect ON contacts(prospect_id);
CREATE INDEX idx_contacts_canon    ON contacts(address_canon);
