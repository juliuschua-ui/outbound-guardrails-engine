# ADVERSARIAL REVIEW — Outbound DM Engine

Three independent red-team passes against the build, run **2026-08-01** after it self-reported success at 916 passing tests.

**The headline:** the 916 tests did not catch a single one of the seven critical/high defects below. Several were certified *by* those tests. That is the point of this document.

---

## Method

Three reviewers ran concurrently with disjoint mandates. None of them wrote any of the code they attacked.

| Lens | Owns the question |
|---|---|
| **Safety & gates** | Can this do something to the real world the operator did not approve? |
| **Measurement honesty** | Can this report a number that is wrong, in a way the operator would believe and act on? |
| **Copy, deliverability & legal** | Can a message go out that damages his reputation, his sending account, his LinkedIn, or exposes him legally? |

Rules of engagement: read-only against the real world, throwaway databases, **no fixes** (a reviewer who patches stops looking), and reproduce-or-discard — every finding needed the exact command, the observed output, and the concrete consequence. Verified *negatives* were required too, so the next reader doesn't re-tread cleared ground.

Each reviewer was told explicitly that the builders wrote their own tests, so the remaining bugs are precisely the ones those tests were blind to.

Each lens found a CRITICAL the other two never went looking for.

---

## Findings and disposition

Severity is the reviewer's. Headline findings were independently re-verified before any fix was dispatched.

### CRITICAL

| # | Finding | Disposition |
|---|---|---|
| **G-C1** | **The shared suppression list was never read.** `suppression.sync_from_file()` had **zero callers** — no CLI command, not in the dispatch table. The engine's only contact with `~/.config/skills/gmail-helper/suppression_list.txt` was the *write* on bounce. All 16 addresses that had said "stop" were invisible, and `dm.suppressed_total` reported a healthy count from the DB while the real list went unread. | **FIXED** — `require_sync()` runs at every gate (import, review, approval, staging). Re-attack verified: address on file → `suppressed AFTER sync: dead_mailbox`. |
| **M-C1** | **`dmctl attrib` booked a demo for any GHL contact that merely string-matched** — no tag, no source, no appointment, no form submission. `normalize_company()` collapsed `Acme Hardscape LLC`, `Acme Landscaping Inc` and `Acme Construction Co` onto `acme`, so a company never messaged was auto-attributed at `high` confidence. `BOUNCED` prospects were in the eligible population, crediting bookings to people who never received the email. With ~1,600 contacts in GHL, the fourth column was synthesized from CRM noise. | **FIXED** — `booking_signal()` now requires explicit evidence (appointment, form source, or booking tag); no signal means no booking, ever. |
| **M-C2** | **Replies that arrived before the message was sent were counted.** No lower time bound anywhere. An inbound stamped 2025-06-15 against a message sent 2026-08-01 produced `new_replies: 1`, `reply_rate=1.0`. Not hypothetical — the operator has cold-emailed this same ICP from this same account since June, so another campaign's replies would land in these variant metrics. At ~4 sends per variant, one carryover reply is a 25-point swing. | **FIXED** — an inbound predating the message it would be credited to is recorded and visible but never counted. |
| **M-C3** | **Three commands reported three different reply counts from one database.** `stats` counted replies over every prospect in the variant's message set but divided by delivered; `report` restricted the numerator to delivered. `dmctl stats` crashed (`wilson: successes cannot exceed trials`) while `status` said 0% and `sheet` showed two replies. Tuned differently: `stats` printed **100%**, `status` printed **0%**. At scale, two variants with genuinely identical performance rendered as **6.7% vs 26.7%**. | **FIXED** — numerator and denominator now drawn from the same delivered population. |
| **C-C1** | **L1 was a 27-entry TLD allowlist**, so 50 of 60 sampled TLDs passed: `bit.ly`, `wa.me`, `goo.gl`, bare IPv4, punycode, every new gTLD. A shortener is the likeliest thing a copy agent emits when told to share a demo — and a shortened link in first-touch cold mail is a top spam signal. | **FIXED** — inverted to a denylist. 16 payloads pinned by test. |
| **C-C2** | **The homoglyph fold omitted Greek α (U+03B1)** while its docstring claimed to fold Greek. `fdαi` renders as "bdai" in Gmail and passed both the linter and the ASCII SQL trigger. Also passing: `fdɑi`, `ꜰᴅᴀɪ`, `FD41`. | **FIXED** — skeleton fold replaces the hand-typed table. |
| **C-C3** | **A merge token that had already rendered passed L5**: `Hey None,` `Hey null,` `Hey Owner,` `Hey Info,` and a company name in the first-name slot. `MIN_HOOK_CHARS = 20` was a length check, so `aaaaaaaaaaaaaaaaaaaa` counted as a hook. This is the most visible automation tell there is, delivered to exactly the skeptical owner this system exists to convert. | **FIXED** — null/role-name guard plus a content check on the hook. |

**One payload defeated three guarantees at once** — a live URL, the product name, and a broken merge token — and produced `lint_report: {"ok": true, "violations": []}` and a would-be Gmail draft. It is now `tests/test_lint.py::test_the_full_red_team_payload_is_refused`.

### HIGH

| # | Finding | Disposition |
|---|---|---|
| **G-H1** | A headless agent could approve outreach. `cmd_approve` checked for a TTY **only when `--from-review` was absent**. A 56-byte file the agent wrote itself, containing no message body, approved a message. Full chain reproduced: review → agent flips the checkbox → approve → stage → real Gmail draft, zero human action. | **FIXED** — approval only applies a file the engine generated (`- batch:` header) and binds per-message. |
| **G-H2** | `dmctl verify --import` laundered a FAIL address into PASS — no signature, no provenance, no host attestation, and `verified_at` stamped `now` unconditionally, so a six-month-old file read as verified today. | **FIXED** — provenance required; stale input visibly degraded. |
| **G-H3** | **Approval bound to the review file's bytes, not the message's.** A file generated for batch `B-ONE` approved a message in `B-TWO` that was never rendered. `touch` defeated the staleness check: body swapped after the human read it, approval succeeded, **and the `state_log` evidence hash was of the file — so the audit trail actively misled about what was approved.** | **FIXED** — `content: sha256:` per block over subject+body, plus batch binding. |
| **G-H4** | A company that unsubscribed got a fresh Message 1 through its website-less twin row: same company under two markets → two prospect rows; row 1 says stop; a later scrape finds `info@` → attaches to row 2 → Message 1 created. The unsubscribe path never wrote a `domain` scope, and G11 is keyed on `prospect_id`. | **FIXED** — a consent withdrawal now writes a domain-scope row (never for freemail providers) and moves matching prospects and their twins to SUPPRESSED, where G12 blocks them. |
| **M-H1** | **The classifier discarded the highest-intent human replies, then blocked the demo email to them.** Of 15 replies a contractor owner would plausibly type, 12 were dropped — systematically the *late, apologetic, high-intent* ones ("sorry, been out of the office on a job all week. what is this about?"). Because `is_human=0`, G7 then refused Message 2, so the prospect who asked "how much?" could never be sent the demo link. | **FIXED** — structural signals outrank body phrase-matching. |
| **M-H2** | `dmctl stats --by-family` routed around the min-n gate: the gate was evaluated on the *pooled* table, so four versions at 20 delivered each produced rates, Wilson intervals and a **`leading`** label. `family_split` was computed and never rendered, so the "40" that opened the gate was two different pieces of copy with no footnote. | **FIXED** — the gate is now evaluated on the un-pooled table regardless of display mode. |
| **M-H3** | A later reply silently erased a recorded demo booking — `outcome` was one slot doing two jobs, so books-then-unsubscribes (a normal sequence) zeroed the booking, silently. In a 20-attempt pilot the single booking vanishes and the variant that actually converted reads zero. | **FIXED** |
| **M-H4** | Real, in-window bookings were silently reported as "organic" — an export lacking `dateAdded` zeroed the entire run, and every booking inside the reconcile lag looked like it predated the send. | **FIXED** — unattributable is now distinct from organic; an out-of-window booking surfaces as a human-confirm card, while one that genuinely predates any possible send is counted separately and never carded. |
| **C-H1** | A phone number was an uncounted second CTA (`ring me on 602-555-0134`, `tel:`, `sms:`) — stronger than the soft question beside it, and `sms:` routes a cold prospect into the channel excluded on TCPA grounds. | **FIXED** |
| **C-H2** | **L6 + L8 + L1 interlocked so that no CAN-SPAM footer could pass lint** — postal address, opt-out line and unsubscribe URL each rejected. Nobody decided this; it emerged from three independently reasonable rules. | **FIXED** — a delimited footer block is now *possible*, exempt from L6/L8. Not mandatory: whether to use it stays the operator's call. |
| **C-H3** | False positives on ordinary copy: `drainage solution` (the words this ICP uses about itself), `thanks,\nthe operator`, `- the operator`, and a rhetorical question in the hook — the last blamed the wrong rule in its error message. | **FIXED** — all four pinned as must-stay-clean tests. |

### MEDIUM / LOW — fixed

Plus-addressing and Gmail dot-folding evading suppression (`john.smith@` vs `johnsmith@` — same mailbox, same human) · the permissive `unique` cap being the **default** with nothing measuring body similarity · `email_gmail._append_pending_action` missing the sandbox guard that had already fabricated a ledger entry once · `--allow-person` documented but not implemented · control characters accepted in an address that would corrupt the shared suppression file · the send-capability scanner missing `api = service.users().messages(); api.send(...)`, the most natural spelling · `dmctl record-reply` ungated, letting one CLI call manufacture the reply that unlocks Message 2 · subject uniqueness beaten by a trailing period · `IndexError` crashing a batch mid-loop on a zero-width company name · `lint_message` failing **open** outside `sequence_step ∈ {1,2}` · emoji range gaps.

### Accepted, not fixed — with reasons

| Finding | Why it stands |
|---|---|
| The cap ledger is per-database, and `DM_ENGINE_DB` is an env var — two DBs allow 60 drafts into one Gmail account | There is no cross-database counter to fix it with (locks are shared, counters are not). Made **loud** instead of silent: the warning goes into stage output *and* `state_log`. |
| Phoenix midnight resets the cap — 20 at 23:58 and 20 at 00:01 | A real queue-shape issue, but the fix belongs in the send scheduler, not the drafting engine. |
| A missing suppression file warns rather than blocks | Hard-blocking would make a fresh machine unusable. The C1 attack (address *on* the file) is closed; an absent file is a different case and is now loudly surfaced. |
| No stagger/window accounting in the engine | Staggering is the send layer's job (DASHBOARD EXEC / Gmail scheduled send), and the engine deliberately owns no send path. Documented in MANUAL.md. |
| `assert_sandboxed()` is test-mode-only | Deliberate: a real `metrics-emit` *should* write to the live dashboard. `DM_ENGINE_DASHBOARD_*_DIR` exists so a rehearsal can be fully sandboxed. |
| browser-helper LinkedIn research caps are documentation, not code | Correctly outside this engine's scope — it runs no browser. Honour-system, and named as such. |
| An owner replying from a personal Gmail is invisible to the scan | Real gap. Needs a thread-id join, which is a design change rather than a patch. Logged for the next pass. |
| A website-less company re-imported under a different market makes a twin row | Market-scoped keys are deliberate. The import **emits a note** naming the twin so a human can merge — surfaced, not silent. Its dangerous consequence (G-H4) is fixed independently. |

---

## What held under attack

Worth recording, because it says where the build was genuinely strong and stops the next reviewer re-treading it.

**The database gates.** Every raw-SQL attack bounced — G2, G3, G4, G7, G8, G9, G11 all fired as documented, including on a bare `sqlite3.connect()` with no engine pragmas and no Python in the path. Forged rows, state jumps, and history rewrites were all refused.

**Concurrency.** Three concurrent stage processes racing a cap of 30 staged exactly 30. Crash-mid-batch rolls the reservation back. The race harness ships with a control that proves a naive implementation *does* breach, so a green result means the guards did the work.

**LinkedIn ToS abstention is real.** No DM-send path, no subprocess, no browser driver, no flag. An AST walk asserts the module imports no networking library at all, and nothing in the docs instructs a human to run something that would automate it.

**Opt-out honoring** was called the strongest part of the system: permanent by design (no removal command exists), person-scoped so a hard-no follows the human to their next company, fail-closed on any error.

**Assignment reproducibility.** The mapping is a pure function of sorted inputs; input file order is irrelevant; membership freezes at first assignment; retiring a variant changes the set hash and produces a loud abort rather than a silent remap.

**Also clean:** no revenue metric anywhere (GHL's zeros stay out); `--force` on `declare-winner` requires a reason and writes a permanent audit row; results are never ordered by a performance column; CSV export raises rather than truncating; SMTP header injection is stopped; the secret scan is clean; and a simulated first commit tracks zero DB, token, or PII files.

---

## The lesson worth keeping

**916 tests passed, and every finding above was live.** Three of them were *certified* by tests that didn't test what their names claimed:

- The adversarial test proving the suppression list was honored **called the unwired function inside its own test body**. It tested the function, not the wiring — while the wiring was the entire defect.
- The "legitimate copy" fixture set was 13 middle-paragraph substitutions into one fixed hook and one fixed close. Every false positive found lived in the hook or the sign-off — the two slots the fixture never varied.
- SC-7 ("every booking carries an attribution method") kept passing while a later reply silently erased the booking, because the row was no longer `demo_booked`.

Two root causes ran underneath most of the findings, and both are worth naming because patching instances would have left the class open:

> **The system validated the shape of its evidence but never its provenance.** A reply was trusted because it existed, not because it arrived after a send. A CRM contact was trusted because a string matched, not because it recorded a booking. A review file was trusted because it existed, not because it bound to the message text.

> **Allowlists were pretending to be denylists.** 27 TLDs and a hand-typed confusables table will always be one entry behind whatever a model emits next.

---

## Current state

```
960 passed, 0 failed
```

Every CRITICAL and HIGH above is fixed and pinned by a regression test using the reviewer's exact attack input. The C1 suppression attack, the copy proof payload, the 16 link bypasses, the 5 product-name spellings, the 9 rendered-token greetings and the 4 phone CTAs were each re-run and verified refused after the fix.

**Recommended before the first real batch:** run one live batch of 5 to your own address end-to-end, confirm the drafts look right in Gmail, and confirm `dmctl sheet` shows what you expect. The engine has never sent anything to a real prospect, and it should not do so on the strength of a test suite alone — that is precisely the mistake this document exists to record.
