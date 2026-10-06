# SUMMARY — Outbound DM Engine

What got built, in one read.

**Built:** 2026-08-01, in a single session — bottleneck diagnostic → plan → build → adversarial review → repair → DM channels.
**State:** 1098 tests passing. First real batches staged 2026-08-09 — 31 drafts and cards created, **nothing sent**.

---

## The problem it solves

An outbound-strategy assessment found the operator's primary bottleneck was a pipeline gap: he had a demo that works and no repeatable way to get the right people into it. Its prescription was to build an outbound system that finds the target customers, writes personalized cold copy in their language, queues it without hand-writing every message, and **measures which message actually breaks through**: variant, recipient, replied, booked. Ship twenty attempts. No guessing.

The measurement half is not a feature bolted on the end. The assessment treated that sheet as the foundation of the whole system, so `dmctl sheet` produces exactly those four columns from a single query, and the honesty of those numbers is what the whole build is organized around.

---

## What it is

A Python + SQLite CLI (`dmctl`). No services, no daemon, no web UI, no third-party runtime dependency, no recurring cost. It reuses the Gmail draft/verify/host-guard scripts, the prospecting pipeline, and the DASHBOARD approvals lane that already existed rather than forking them.

**DM-first, four channels:**

| Channel | Ends as | Daily cap (template / unique) |
|---|---|---|
| WhatsApp | copy-ready card → you send | 8 / 12 |
| Messenger | copy-ready card → you send | 12 / 18 |
| LinkedIn | copy-ready card → you send | 20 / 30 |
| Email | Gmail draft → you release | 20 / 30 |

```
prospects.json → import · dedup · suppression
      ↓
qualify → batch → deterministic variant assignment
      ↓
compose job → copywriting agent → lint (12 mechanical rules)
      ↓
human review file → YOU approve
      ↓
stage: copy-ready card, or a Gmail draft
      ↓
╔═══════════════════════════════════════╗
║  YOU send. The engine never does.     ║
╚═══════════════════════════════════════╝
      ↓
reconcile · classify replies · attribute bookings → sheet · stats · metrics
```

It does **not** find prospects (lead-sourcing agents do) and does **not** write the copy (an agent does, against a job spec). It owns the disciplined middle.

## The five guarantees

Each enforced mechanically, each with a test proving the enforcement fires.

1. **Nothing sends without you.** No send code exists — an AST scan enforces it, with a control test that injects `drafts().send()` to prove the scanner has teeth. Transmission lives outside the repo, so every message clears two independent human gates.
2. **No link or product name in a first-touch message.** 12 lint rules plus a SQL trigger, over text that has been NFKC-normalized and homoglyph-folded first.
3. **A suppressed person is never contacted again.** Address-, domain- and person-scoped, fails closed, synced from the shared list at every gate. Identity folds per channel — phone punctuation, Gmail dots and `+tags`, Page URL fragments and query strings. A hard-no on one channel suppresses that human on all of them.
4. **Variant results are reproducible.** Deterministic assignment that records its hashes and aborts on drift.
5. **Small samples are reported as small.** Below 30 delivered per variant and 5 human replies, raw counts and verbatim replies only — no winner, no rates dressed as conclusions, and `retire` mechanically refuses.

---

## How it was built

| Phase | What ran |
|---|---|
| **Plan** | One Fable-5 architect produced a 1,046-line plan and no code — complete enough that five builders could work in parallel without talking to each other. |
| **Build** | Five Opus-5 builders on disjoint file sets. Owner A (core, DB, triggers, shared fixtures) ran first because everyone imports it; B–E ran concurrently against its finished interfaces. |
| **Integrate** | Closed the seams, then actually ran the thing — which immediately surfaced a bug 903 passing tests had not. |
| **Review** | Three independent red-teamers, distinct lenses, none of whom wrote any of the code. |
| **Repair** | Three fixers by lens; all three hit a session limit mid-edit, leaving the repo broken at 28 failures with no git history to revert to. Repaired forward by hand. |
| **Pivot** | The user clarified DM-first meant WhatsApp and Messenger, not LinkedIn. Two adapters added, plus a channel-aware identity layer. |

---

## What the adversarial review found

**916 tests passed, and none of them caught the seven critical/high defects below.** Three were actively *certified* by tests that didn't test what their names claimed.

- **The engine never read the real suppression list.** `sync_from_file()` had zero callers. All 16 addresses that had said "stop" were invisible, while the dashboard reported a healthy suppression count. The adversarial test proving this worked called the unwired function *inside its own test body*.
- **`dmctl attrib` booked a demo for any GHL contact that merely string-matched** — no tag, no source, no appointment. With ~1,600 contacts in GHL, the fourth column was being synthesized from CRM noise.
- **Replies that arrived before the message was sent were counted.** the operator has emailed this same ICP from this same account since June, so another campaign's replies would have landed in these metrics.
- **The linter passed a live URL, the product name, and `Hey None,` in one message** — `bit.ly` sat outside a 27-entry TLD allowlist, and the homoglyph fold omitted Greek α.
- **An agent could approve outreach**, using a review file it wrote itself, bound to nothing.
- The classifier discarded the *highest-intent* replies and then blocked the demo email to them; `--by-family` routed around the honesty gate; a later reply silently erased a recorded booking.

All fixed, each pinned by a regression test using the reviewer's exact attack. Full detail and the accepted-not-fixed list: **[ADVERSARIAL-REVIEW.md](ADVERSARIAL-REVIEW.md)**.

Two root causes ran underneath most of it: **the system validated the shape of its evidence but never its provenance**, and **allowlists were pretending to be denylists**.

## What running it found that tests did not

Three real bugs surfaced the first time the pipeline was driven end to end against live data — after the review, on a green suite:

- `metrics-emit` had no safe path: it either wrote to the live DASHBOARD dashboard or refused outright, so a rehearsal was impossible.
- **Messenger canonicalization kept `#` and `?`** — a profile URL copied from a browser ends in `#`, so a hard-no on the bare URL would not have matched it.
- **Assignment ignored channel reachability** — a WhatsApp batch claimed a Messenger-only prospect, which then blocked at compose and could never join the batch that could reach it. Silently costs a prospect, and would have bitten immediately in real use, since most contractors have a phone *or* a Page, not both.

Then the first *real* batches (2026-08-08/09) added three more, none of which 1096 passing tests could see:

- **Every compose job blocked on a missing `company_casual`.** No import path ever populated it, so the very first real compose stopped dead on all 8 batches at once. The field was mandatory and unfillable in the same breath.
- **`decide` reported failure on success.** It returned `EXIT_PARTIAL` if *any* row in the review file was skipped — but `approve_from_review()` walks the whole file, so on every real multi-message batch the not-yet-decided rows always skip. DASHBOARD rendered that as an error on every single click. 31 messages were approved while the operator was being told, each time, that nothing had happened. Every prior `decide` test used a single-message batch; a one-row fixture cannot express this bug.
- **A decided card looked identical to a pending one.** The APPROVE button carried a `disabled` attribute and no disabled styling, so re-clicking a spent card did nothing, silently — indistinguishable from a broken button.

The last two share a root cause worth naming: **the engine was correct and said so unconvincingly.** Both were failures of reporting, not of logic, and both cost more operator trust than an actual crash would have.

---

## Deliberate constraints

- **No automated DMs on any platform.** Meta's Business APIs forbid cold business-initiated messaging; LinkedIn automation risks the account. A human sending from their own account is not an API flow — so the engine writes and you send.
- **No cold SMS.** TCPA.
- **No revenue metric.** Every GHL opportunity carries `monetaryValue: 0` and nothing has ever been Closed Won — a "$0 pipeline" reading is worse than no reading.
- **No auto-optimization.** Assignment stays deterministic and dumb until the sample size justifies anything cleverer.
- **National territory.** Market is a parameter; the engine is not scoped to Arizona.

## Where it stands

Working, reviewed, and carrying its **first real batches**.

| | |
|---|---|
| Prospects | 66 sourced — 55 qualified, 11 excluded as outside ICP |
| Markets | Phoenix AZ · Austin TX · Tampa FL · Savannah GA |
| Messages | 47 composed and linted across 8 batches |
| **Staged** | **31** — 20 Gmail drafts, 11 Messenger cards |
| Still approved, not staged | 15 (email daily cap reached) |
| **Sent** | **0** |

The artifacts now exist: 20 real drafts sitting in Gmail, 11 copy-ready cards in `data/outbox/messenger/`. Staging is where the caps, the last-moment suppression re-check, the verification verdicts and the Gmail-Sent dedup all actually fire — and they did: email stopped dead at exactly 20, its documented template-body daily cap, leaving 15 approved messages for the next day rather than quietly exceeding it.

**Sent remains 0, and that is the row that matters.** Every one of those drafts still requires the operator to press send, from Gmail or from his phone. The engine cannot do it — there is no send code in the repository.

The self-addressed DM smoke test (two messages to the operator) that preceded this remains the right pattern: the engine should not touch a live prospect on the strength of a test suite alone. That is exactly the mistake this build already made once and had to be told about by an outside reviewer — and the first real run promptly surfaced three more defects a green suite had certified as fine.

Start with **[MANUAL.md](MANUAL.md)**.
