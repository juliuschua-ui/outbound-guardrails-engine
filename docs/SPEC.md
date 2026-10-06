# SPEC — Outbound DM Engine

What the system is contractually required to do, and what it is required never to do.

- **PLAN.md** describes *how* it was designed. **MANUAL.md** describes *how to run it*. This document is *what it must be true of*.
- Requirements are normative: **MUST** / **MUST NOT** / **MAY**.
- Every requirement here has at least one test. 1098 tests total; `tests/adversarial/test_gates.py` carries the invariant suite.

---

## 1. Purpose and success criteria

The system exists to discharge one bottleneck: skeptical buyers, too little demo traffic, and no repeatable outbound motion. It must let the operator ship outreach at a repeatable cadence and learn, from evidence, which message breaks through.

| ID | Success criterion | Verified by |
|---|---|---|
| SC-1 | 20 outreach attempts can be composed, gated, staged and recorded without hand-writing each message | Walking skeleton, every channel |
| SC-2 | The four-column sheet (variant · recipient · replied · booked) falls out of one query | `sheet` view + `dmctl sheet --csv` |
| SC-3 | Per-variant reply and booking counts are attributable and reproducible | Pinned assignment hash; drift-abort |
| SC-4 | No message reaches a prospect without explicit human release | State machine + triggers + AST scan |
| SC-5 | No Message 1 carries a link, product name, or pitch | 12 lint rules + trigger `trg_msg1_no_pitch` |
| SC-6 | No unverified or FAIL-verified address reaches a draft | `trg_email_verify_gate` + stage-time check |
| SC-7 | A booking is never attributed on evidence weaker than its recorded confidence | Attribution ladder + manual-confirm cards |
| SC-8 | Statistically meaningless results are reported as meaningless | Min-n gate; `retire` refuses below it |
| SC-9 | Zero marginal cost | No paid services; SQLite + stdlib |

The originating report set the bar at 20 attempts. **At n=20 the deliverable is the verbatim replies, not a percentage.** Any presentation implying otherwise violates SC-8.

---

## 2. Hard invariants

Violating any of these is a defect regardless of how convenient the violation is.

### I-1 — The engine MUST NOT contain send capability
No module under `src/` may call `.send()`, `.sendmail()`, `.send_message()`, `.sendmsg()`, or import `smtplib`. Enforced by an **AST scan** (not grep — comments and docstrings cannot trip it, and code cannot hide in them), with a control test that injects `drafts().send()` and `import smtplib` into a copy of the tree and asserts both are caught.

Transmission happens outside this repo: the DASHBOARD EXEC send-queue, VPS `send_*.py` scripts, or a human. This is a second, independent gate — not redundancy, but separation of powers.

### I-2 — Every message MUST pass a human gate
State machine, exhaustively enumerated in `state.MESSAGE_TRANSITIONS`:

```
COMPOSED ──→ READY ──→ APPROVED ──→ STAGED ──→ SENT ──→ REPLIED ──→ CLOSED
    │           │          │           │         └────→ BOUNCED ──→ CLOSED
    ├→ LINT_FAILED ─→ COMPOSED (re-ingest)
    └→ CANCELLED    └→ REJECTED
```

Deliberately absent and individually tested: `READY→STAGED` (skips approval), `COMPOSED→APPROVED/STAGED/SENT`, `APPROVED→SENT`, `STAGED→APPROVED`, `SENT→` anything upstream, `REJECTED→APPROVED`, and all self-edges. Terminal: REJECTED, CANCELLED, CLOSED. `SENT` MUST carry evidence before any write.

The legal edge set is restated by hand in `test_state.py::EXPECTED_MESSAGE_EDGES`, so widening the machine fails a test.

Approval MUST come through one of exactly two entry points, and both run the identical gate — content-digest binding, staleness check, suppression re-check:

| Command | Proof of a human | Refuses |
|---|---|---|
| `approve` | a TTY | headless, always; no `--yes`, `--force` or env override exists |
| `decide` | the caller's own authentication, upstream | nothing by itself — see below |

`decide` (added 2026-08-09 for the DASHBOARD review lane) does **not** weaken I-2, and the distinction matters enough to write down. It flips one checkbox in the review file exactly as a hand-edit would, then calls the same `approve_from_review()` `approve` calls. Every protection survives; only the proof of a human changes. It is reachable by anything holding DASHBOARD's bearer token — the same trust level that layer already grants EXEC dispatch and the send-queue arm, both of which are strictly more dangerous. What it may never do is *claim* to be a human: `--actor` is REQUIRED with no default, uniquely among actor-taking commands, so `state_log` can never read "operator" for a decision a program made.

The original wording of this invariant — "it MUST refuse headless" — is now true of `approve` only. That is a deliberate, bounded relaxation, not an erosion: the engine still contains no send code, so the furthest a headless approval can travel is a Gmail draft or an outbox card, both of which still require a human to actually transmit.

### I-3 — Message 1 is pain-only
No URL in any form, no product name (`BDAI`, `Bright Desk AI`, `BrightFlow Cure`, `brightdesk`), no pitch. Text is normalized (NFKC → invisible-character strip → **Cyrillic/Greek homoglyph fold** → quote/dash fold) *before* any rule runs, and the normalized form is what is stored — so the SQL trigger's ASCII `LIKE` patterns see the same text the Python linter did.

### I-4 — Suppression is permanent and fails closed
A suppressed address, domain, or person MUST NOT be contacted again, on any channel, in any batch. `is_suppressed()` returns a reason on any error, unknown channel, or empty input — an inability to determine status is treated as suppressed. Re-checked at stage time, backed by `trg_msg_suppressed`.

**Identity is per channel.** `canonical_email()` is meaningless for a phone number, so `canonical_address(channel, address)` dispatches and migration 005 mirrors all three forms inside `contacts.address_canon`, so the Python pre-check and the SQL trigger can never disagree about who is suppressed:

| Channel | Canonical identity | So these are one destination |
|---|---|---|
| email | mailbox: `+tag` folded everywhere, dots folded on gmail.com | `owner+quote@x.com`, `owner@x.com` |
| whatsapp | digits, US country code dropped (`attribution.normalize_phone`'s convention) | `+1 (602) 555-0134`, `602-555-0134`, `16025550134` |
| messenger | the Page/profile handle, host and scheme stripped | `m.me/x`, `facebook.com/x`, `fb.me/x` |

Scope semantics, also mirrored in `trg_msg_suppressed`:

- `address` and `domain` scope stay on their own channel — a dead mailbox MUST NOT mute the company's Page.
- `person` scope crosses **every** channel. It records a fact about a human, not about one app, and the operator reaches the same owner on WhatsApp, Messenger and email.
- `domain` scope MUST NOT apply to whatsapp or messenger. There is no domain in a phone number or a page handle; `add()` refuses to write such a row, `is_suppressed()` refuses to read one, and consent-withdrawal propagation writes no domain row on those channels. Reaching the company's other addresses is not lost — the prospect still moves to SUPPRESSED, where `trg_msg_prospect_suppressed` blocks every channel.

Appends to the shared suppression file MUST be bare addresses under a `# --- dm-engine: <reason> ---` header. An inline comment (`addr # bounce`) would silently break `create_drafts.load_suppression()`, which reads the line verbatim.

### I-5 — Assignment is deterministic
`sorted-hash-rr-v1`. Pure function of (sorted membership, variant set, batch sequence). Records `assign_hash` and `variant_set_hash`. Recomputation MUST yield identical results; divergence MUST abort before opening a write transaction, never partially reassign. Batch membership freezes at first assign, so a later import into the same market cannot make a legitimate re-run look like tampering.

### I-6 — The daily cap holds under concurrency
≤10 per window, counted **per channel** on a fixed-UTC-7 date. Enforced by `locks.hold()` + `BEGIN IMMEDIATE`, reserving the slot *before* the artifact is created so a failure rolls the reservation back. Verified with two real subprocesses, plus a control test proving a naive count-then-insert does breach.

| Channel | Template body | Uniquely written | Why |
|---|---|---|---|
| email, linkedin | 20/day | 30/day | the operator's cadence rule; a sender reputation recovers, slowly |
| messenger | 12/day | 18/day | Page-level rate limiting and the "message requests" folder |
| whatsapp | 8/day | 12/day | Bans are spam-*detection* driven and cost the number itself — the one on the vans |

`effective_personalization` applies on every channel: a batch of near-identical bodies takes the template cap however it declared itself. On WhatsApp that matters most — near-identical text is precisely what the detection reads.

### I-7 — Statistics MUST NOT overstate
- Reply rate = human replies ÷ **delivered** (sent − bounced). Never ÷ sent, ÷ staged, or ÷ imported.
- Autoresponders, out-of-office, delivery-delay notices and NDRs are **not** human replies.
- Below the gate (default: 30 delivered on the smallest arm **and** ≥5 human replies across the experiment): raw counts and verbatim replies only. No winner, no leader, no rate presented as a conclusion, no ordering that implies ranking.
- Above the gate: Wilson score intervals. A variant is leading only if its lower bound clears **every** rival's upper bound.
- No revenue or pipeline-value metric, ever — GHL `monetaryValue` is uniformly 0 and a "$0 pipeline" reading is worse than no reading.
- `report.py` contains **zero division nodes**; all rate arithmetic delegates to `stats.rate`.

### I-8 — Nothing is silently dropped
Every imported prospect and every ingested draft MUST land in exactly one counted bucket. `ready + failed + len(unknown) == len(drafts)` is asserted. Truncation, dedup-away, and skip MUST be reported in a count a human reads.

### I-9 — No automated DMs on any channel
`linkedin_manual.py`, `whatsapp_manual.py`, `messenger_manual.py` and the `manual_dm.py` staging loop they share MUST contain no DM-sending path, not behind a flag, and no `requests` / `urllib` / `http` / `playwright` / `selenium` / `socket` / `subprocess` import. They stage copy-ready human-send cards; the operator is the transport, which makes the second release gate inherent.

The reasons are per channel and all terminal:

| Channel | Why there can be no send path |
|---|---|
| LinkedIn | Automated DMs violate the User Agreement §8.2. The operator has one account; losing it kills the channel permanently. |
| WhatsApp | The Business Platform prohibits cold business-initiated messaging — an outbound conversation needs prior opt-in **and** a pre-approved template. On the consumer app, bans are spam-*detection* driven (volume, velocity, near-identical text, low reply rate) and cost the phone number itself. |
| Messenger | The Messenger Platform only permits a Page to reply inside a 24-hour window the **user** opens. There is no compliant API call for a cold first touch. |

The engine holds no Meta token and never asks for one.

### I-10 — No cold SMS
TCPA. `--channel sms` is refused with exit 2.

---

## 3. Data model

14 tables + 1 view (`data/migrations/001_init.sql`, `003_views.sql`; `005_dm_channels.sql` widens four channel CHECK constraints and makes `contacts.address_canon` channel-aware):

| Table | Holds |
|---|---|
| `prospects` | company identity, market, ICP note, state |
| `contacts` | addresses per prospect, per channel, with source and verify verdict |
| `experiments` / `variants` | the message experiment and its immutable versioned variants |
| `batches` / `assignments` | a run of outreach and its deterministic variant mapping |
| `messages` | the copy, its state, its lint report |
| `stage_events` | one row per staged message — the cap ledger |
| `replies` / `outcomes` | classified inbound, and demo bookings with attribution method |
| `suppression` | do-not-contact, scoped by address / domain / person, per channel |
| `state_log` | append-only audit of every transition |
| `runs` | batch-level run records |
| `sheet` (view) | **the four columns the outbound-strategy report asked for** |

`prospect_key` is market-scoped. **Known consequence:** a website-less company re-imported under a different market string produces a second prospect row. `import_prospects` emits a note naming the twin and its market so a human can merge — it is surfaced, not silent.

---

## 4. The database gates (G1–G12)

Twelve triggers in `002_triggers.sql`. They exist so the guarantees hold against a **raw `sqlite3` write**, not just against the Python API — the realistic "poking the DB directly at 2am" case.

| Trigger | Enforces |
|---|---|
| `trg_msg_born_composed` | A message can only be born COMPOSED |
| `trg_msg_approve_path` | READY→APPROVED only |
| `trg_msg_stage_path` | APPROVED→STAGED only |
| `trg_msg_sent_path` | STAGED→SENT only, evidence required |
| `trg_msg_ready_lint` | No READY without a passing lint report |
| `trg_msg1_no_pitch` | No URL / product name in a step-1 message |
| `trg_msg2_reply_gate` | No Message 2 to a prospect who never replied |
| `trg_msg_suppressed` | No message to a suppressed contact, on any channel, in any spelling |
| `trg_email_verify_gate` | No FAIL / unverified / guessed-RISK address |
| `trg_statelog_no_update` / `_no_delete` | The audit log is append-only |
| `trg_one_msg1_per_prospect` | One first-touch per prospect, ever |

**Verified by mutation, not inspection.** Each trigger was removed in turn and the suite re-run; all twelve produce failures when absent. One test proves the gates hold on a bare `sqlite3.connect()` with no engine pragmas and no Python in the path.

> A trigger caught during the build had shipped as `json_extract(...) != 1`. For a lint report missing an `ok` key that evaluates to NULL, the `WHEN` clause is falsey, and **the trigger silently does not fire** — an unlinted message would have reached READY. Now `IS NOT 1`. This is why guards are mutation-tested.

---

## 5. Lint rules (L1–L12)

Applied after normalization. `lint.RULES` is the machine-readable source.

| ID | Catches | Scope |
|---|---|---|
| L1 | URLs: `http(s)://`, `mailto:`, `www.`, markdown, angle-bracketed, bare domains (uppercase TLD, trailing punctuation), obfuscated `x[dot]com` / `x dot com` / `x . com` | step 1 |
| L2 | Product identity: `bdai`, `f.d.a.i`, `bright desk ai`, `brightdesk`, `brightflow`, `brightflow cure` | step 1 |
| L3 | Banned phrases: *i'd love to connect*, *synerg\**, *circle back*, *touch base*, *just checking in*, *hope this finds you well*, *revolutioniz\**, *game-chang\**, *cutting-edge*, *streamlin\**, *solution(s)*, *hey there* | all |
| L4 | Emoji (8 codepoint ranges), subject and body | all |
| L5 | Unresolved placeholders `{}` `[]` `<>` `%%` `$` and literal token names — **plus semantic holes**: empty first line, `Hi ,`, space-before-punctuation, double comma, hook under 20 chars, hook starting mid-clause | all |
| L6 | Long-form shape: ≤120 words, subject present + entirely lowercase + ≤60 chars, signature exactly `the operator` alone on the last line | email |
| L7 | Short-form shape: sentence cap (abbreviation- and decimal-guarded), character cap, no subject | linkedin, whatsapp, messenger |
| L8 | One CTA: exactly one `?`, and it closes the message | step 1 |
| L9 | Subject uniqueness — case/whitespace/unicode-folded, within batch and against 30 days of history | email |
| L10 | `company_casual` sanity: legal suffix, ALL-CAPS, >4 words, trailing punctuation, missing | all |
| L11 | Step 2 carries the demo link exactly once and greets by real first name | step 2 |
| L12 | Fake urgency: *act now*, *limited time*, *last chance*, *don't miss* | all |

**False positives are a first-class failure.** A linter that rejects good copy trains its operator to bypass it, after which it protects nothing. 13 legitimate-copy cases plus all five seed variants MUST lint clean. Two rules were deliberately narrowed and documented: `x . com` excludes the `ai` TLD (so *". ai is not the issue"* is not a link), and `brightflow` requires no space (so *"bright flow"* is allowed). L3 masks `company_casual` out of the haystack, so a prospect actually named *Sunset Solutions* is not a banned word.

---

## 6. Interface contracts

**Prospect import** (produced by lead-sourcing agents):
```json
[{"company_name":"…","website_url":"…","us_state":"AZ","owner_first_name":"…",
  "icp_fit_note":"…","hook_fact":"…","company_casual":"…",
  "emails":[{"address":"…","source":"website"}],
  "linkedin":[{"address":"…","source":"linkedin"}],
  "whatsapp":[{"address":"+1 (602) 555-0134","source":"website"}],
  "messenger":[{"address":"https://m.me/…","source":"website"}],
  "source":"lead-scout","source_detail":"…"}]
```
All four address arrays take the same `{"address": …, "source": …}` shape and all are optional. `source` is load-bearing: it decides whether a RISK verdict may draft, so an **absent** source falls through to `format_guess`, the most distrusted class — never to a verified one.

A `whatsapp` address is stored canonicalized (`+1 (602) 555-0134` → `+16025550134`), because a phone number has one written form and storing it is what makes `UNIQUE (channel, address)` dedup two spellings. A `messenger` address is stored as found — the card is a link a human clicks — and its handle becomes the `person_key`. An entry the import cannot use is reported in `notes`, never silently dropped (I-8).

**Draft ingest** (produced by `outreach-specialist` from the compose job):
```json
{"batch_id":"B-…","drafts":[{"prospect_key":"…","channel":"email",
  "sequence_step":1,"subject":"…","body":"…"}]}
```

**DASHBOARD metrics** — `dm.*` keys only, no revenue key. Every rate ships alongside its denominator (`dm.delivered_total`, `dm.bounced_total`) so a dashboard cannot render a rate without its n.

---

## 7. Environment requirements

| | |
|---|---|
| Runtime | Python 3.14, stdlib only. No third-party runtime dependency. |
| Storage | SQLite (WAL, `foreign_keys=ON`, `busy_timeout=5000`) |
| Sending account | `sam.taylor@example.com` — personal, better cold deliverability. The northwindmedia account MUST NOT be used for cold outreach. |
| Send host | VPS `send-host`. WSL is draft-only. |
| Scheduling | ⚠️ `scheduledSendTime` **does not exist** in the Gmail API — it is silently ignored and the message sends immediately. Use Gmail native scheduled send or the DASHBOARD EXEC queue. |
| Secrets | `.env` + committed `.env.example`; `.gitignore` before any secret lands. No hardcoded credentials. |

**Test isolation MUST be mechanical, not aspirational:** `DM_ENGINE_TEST=1` session-wide, a `sys.meta_path` blocker rejecting Google library imports, `get_service()` raising `TestModeError`, `load_config()` refusing a db path outside repo/tmp, and `report.assert_sandboxed()` refusing writes outside repo/tmp.

> **Known limitation:** `assert_sandboxed()` is test-mode-only by design. It protects test runs, not operator runs — a real `metrics-emit` writes to live DASHBOARD on purpose. `DM_ENGINE_DASHBOARD_METRICS_DIR` and `DM_ENGINE_DASHBOARD_APPROVALS_DIR` exist so a rehearsal can be fully sandboxed.

---

## 8. Non-goals

Deliberately not built. Adding any of these is scope creep, not improvement.

1. Any send code, ever — not behind a flag
2. Automated DMs of any kind — LinkedIn, WhatsApp, Messenger, Instagram — or connection requests
3. Cold SMS
4. A web UI — DASHBOARD is the surface
5. A scheduler or daemon inside the engine
6. An ORM, Pydantic, or any third-party runtime dependency
7. Email-infrastructure work (warm-up, new domains, DKIM)
8. GHL writes of any kind — read-only, from disk exports
9. Revenue or pipeline-value math
10. Multi-user auth, VPS↔WSL DB sync, historical backfill
11. Bayesian bandits or auto-optimization — assignment stays deterministic and dumb until n justifies more
12. Paid data sources
