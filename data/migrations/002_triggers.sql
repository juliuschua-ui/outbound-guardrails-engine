-- 002_triggers.sql — invariants enforced INSIDE the database. From PLAN.md §3.3.
-- These make the gates hold even against a direct `sqlite3` write. state.py is the
-- friendly layer on top; these are the law. Every one has a firing test in
-- tests/test_triggers.py.

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
-- DEVIATION from PLAN §3.3 (logged): the plan wrote `json_extract(...) != 1`, which
-- yields NULL — i.e. does NOT fire — for a lint_report that is valid JSON but has no
-- "ok" key (e.g. '{}'). `IS NOT 1` is the null-safe form and closes that hole.
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
