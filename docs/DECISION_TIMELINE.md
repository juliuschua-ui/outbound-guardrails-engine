# DECISION TIMELINE — Outbound DM Engine

The whole project in order: what happened, what was decided, why, and what was rejected. Written so that the question "why is it like this?" has an answer six months from now instead of a guess.

Everything below happened on **2026-08-01**, in one session. This replaces an earlier decision log that covered only phases 0–5; it is now the single timeline.

**Reading it fast:** phases 0–2 are how it was scoped, 3 is the build, 4–5 are where it nearly went wrong, 6–7 are the pivot to DM-first. The most useful phases are **4 and 7** — the review, and what running the thing for real turned up that no test did.

---

## Phase 0 — Origin

### D-01 · The trigger
The operator received an outbound-strategy assessment from a third-party consultant. It named his primary bottleneck as a pipeline gap and his highest lever as an AI-assisted outbound system that finds where the target customers spend time online, drafts personalized cold DMs or emails in their language, and queues the outreach.

**Decision:** build it, scoped to what the assessment specifies, including the measurement layer, which it treats as the foundation of the whole system rather than an add-on.

**Why it matters:** the assessment's success bar is concrete and low — 20 outreach attempts, reply rate and demo bookings tracked per message variant. Everything traces to one of its sections. Scope creep past that bar is a defect, not a bonus.

**What was missed here, and cost a pivot later:** the report said "cold DM **or** email." Nobody asked *which DM platform*. See D-30.

---

## Phase 1 — Reconnaissance

### D-02 · Survey before design
Scanned the existing stack before writing any plan: `~/.config/skills/`, `~/work/BDAI/`, `~/work/dashboard/`, the GHL/Apollo MCP surface.

**Found:** much of the system already existed in pieces — a Gmail draft/verify/send-guard script library, a 5-prospect-per-market outreach skill, a Playwright browser automation layer, a personal ops dashboard with an approvals lane and a metrics store.

**Decision:** the engine **generalizes and instruments** existing infrastructure rather than replacing it.

**Rejected:** a greenfield build. It would have duplicated a working, battle-tested Gmail layer — including its SMTP verification gate and host guard — and created two code paths that could drift, the more dangerous of which would be the one nobody was watching.

### D-03 · Project location and doc set
`the repo root`, matching the `BDAI/deploy-automation/` precedent: BDAI root stays non-git, subprojects are their own repos, each carrying README / PLAN / SPEC / MANUAL / adversarial results. House style already existed; a new convention would have been a second thing to remember.

---

## Phase 2 — Planning

### D-04 · A dedicated architect pass, separate from the build
A single Fable-5 agent produced `docs/PLAN.md` (1,046 lines) as its only output. It wrote no production code.

**Why:** the plan had to be complete enough that five builder agents could implement **in parallel without talking to each other**. That only works if every cross-module interface is written down in advance. Splitting "decide" from "do" is what made the parallel build possible.

**Constraint given:** read the real files and quote real signatures — no invented APIs. It read `create_drafts.py`, `verify_emails.py`, `_sender_host_guard.py`, the DASHBOARD send-queue and metrics contracts, the existing suppression list, and the deploy-automation precedents.

### D-05 · Python + SQLite, no services
Plain Python 3, a SQLite file, a CLI. No web framework, no ORM, no queue broker, no daemon, no third-party runtime dependency.

**Why:** the assessment's constraint is effectively a zero budget. Every added service costs money, breaks silently, or needs to be running when nobody is looking. SQLite is a file, and a file is inspectable with `sqlite3` at 2am.

**Rejected:** Postgres + a small web UI. DASHBOARD already is the dashboard; a second one is a second place for the truth to live.

### D-06 · The engine contains **zero send code** — architectural invariant
No module may call `drafts().send()`, `messages().send()`, or SMTP. The engine composes, lints, queues and stages. Transmission stays where it already lives.

**Why:** the standing rule is "never auto-send." Enforcing that by *policy* means one careless edit breaks it. Enforcing it by *architecture* means the capability isn't present to misuse — and it's mechanically testable, so a test can fail the build if anyone adds it.

**Consequence:** every message is gated twice, by two independent systems.

### D-07 · Channel automation ruled out on ToS grounds
`browser-helper` is used for read-only research. Messages are staged as copy-ready human-send cards. No automated-DM code path exists, not behind a flag.

**Why:** automated LinkedIn DMing violates ToS and risks permanent account loss. That account is not a fungible asset.

**Rejected:** cold SMS entirely — TCPA, and the standing rule requires documented express written consent.

### D-08 · Deterministic variant assignment, not random-at-send-time
`sorted-hash-rr-v1` — a reproducible sorted-hash round-robin that records its hashes and **aborts on drift**.

**Why:** if assignment isn't reproducible, every per-variant number is unfalsifiable. The whole point is "no guessing, no vibes" — which requires that the data can be re-derived.

### D-09 · Honest statistics, mechanically enforced
Below a minimum-sample gate (30 delivered per variant **and** ≥5 human replies), raw counts and verbatim replies only. No winner. `retire()` mechanically **refuses** without an explicit `force=True`.

**Why:** the report asks for 20 attempts. At n=20 nothing is significant. The most expensive failure this system could produce is not a crash — it's a plausible "winning variant" that is pure noise, sending the operator to double down on a message that isn't working.

### D-10 · Attribution is honest about a missing join key
**Problem found during planning:** the demo form collects first name, phone, company name, company type, website — **no email address**. There is no clean key joining a sent message to a booking.

**Decision:** a confidence ladder — UTM `utm_content` → domain match → name+phone → manual-confirm card. Anything below high confidence becomes a card rather than a silent booking.

**Why:** a fabricated attribution is worse than an unattributed booking, because it makes a variant look like a winner on evidence that doesn't exist.

### D-11 · GHL is read-only, from disk, and emits no revenue metric
**Measured:** all 30 open opportunities carry `monetaryValue: 0`; nothing has ever been Closed Won; a limit=100 query returns ~61,500 characters and overflows context.

**Decision:** consumed only as a chunked disk export, never written to, and **no** pipeline-value metric. An absent metric reads as "unknown"; a zero reads as "we measured, and it's nothing."

### D-12 · Territory is national
Market is a parameter. Glendale and Scottsdale AZ were the first two test batches only — a drift the operator corrected the same day after 42 agent charters had quietly narrowed to them. The engine must not re-introduce it.

---

## Phase 3 — Build

### D-13 · Sequenced Owner A, parallelized B–E
Five Opus-5 builders, partitioned so **no two write the same file**. Owner A (config, DB, migrations, triggers, shared test fixtures) ran to completion first; B–E then ran concurrently against its finished, verified interfaces.

**Why:** A is the foundation every module imports, and its `conftest.py` defines the fixture names the others write tests against. Running A in parallel would have had four agents coding against a moving interface.

### D-14 · Project-local venv with system site packages
System Python is PEP 668 externally-managed. A plain venv would have lost `googleapiclient`, which the gmail-helper scripts need. `--system-site-packages` keeps them visible while giving pytest somewhere legal to live.

### D-15 · A real SQL bug caught inside the trigger layer
Trigger G5 as specified read `json_extract(NEW.lint_report,'$.ok') != 1`. For a lint report missing an `ok` key, `json_extract` returns NULL, `NULL != 1` is NULL, the `WHEN` clause is falsey — **and the trigger does not fire.** A message with no passing lint would have reached READY.

Shipped as the null-safe `IS NOT 1`, with six parametrized cases plus a malformed-JSON case that fails closed.

**Why it's logged:** this is the exact class of bug the trigger layer exists to prevent, hiding *inside* the trigger layer. It was found because the build was told to verify each trigger **fires**, not that each trigger exists.

### D-16 · Triggers verified by mutation, not inspection
Each `CREATE TRIGGER` was removed in turn and the suite re-run, confirming every one produces failures when absent. The file was then restored and diff-verified byte-identical.

**Why:** a trigger that exists but never fires is worse than none — it manufactures confidence. One test additionally proves the gates hold on a bare `sqlite3.connect()` with no engine pragmas and no Python in the path, which is the realistic "poking the DB directly at 2am" case.

### D-17 · Integration refused to weaken a gate to satisfy a doc
PLAN §11's Phase-0 block implies `stage --dry-run` then `mark-sent` works. It cannot: dry-run deliberately changes no state, and `APPROVED → SENT` is not a legal transition.

**Decision:** the gate stays; the *doc* was wrong. Marking SENT a message that was never staged would put a lie in the measurement sheet, and the sheet being true is the entire value.

### D-18 · `metrics-emit` had no safe path — found by actually running it
In normal mode it wrote to the live DASHBOARD dashboard; under test mode the sandbox guard refused outright. There was no third option, so a rehearsal was impossible. Fixed with `DM_ENGINE_DASHBOARD_METRICS_DIR` / `_APPROVALS_DIR` overrides.

**Why it's logged:** this bug was invisible to 903 passing tests and surfaced the moment someone ran the thing end to end. Walking skeletons earn their keep.

---

## Phase 4 — Adversarial review

### D-19 · Three independent red-teamers, distinct lenses
Rather than one review pass: three concurrent reviewers with disjoint mandates — **safety/gates**, **measurement honesty**, **copy compliance/legal** — each told explicitly that the builders wrote their own tests, so the remaining bugs are precisely the ones those tests were blind to.

**Why:** a single reviewer converges on one mental model and re-treads the builders' assumptions. Diversity of lens catches failure modes redundancy cannot. It worked: **each lens found a CRITICAL the other two never went looking for.**

**Rules of engagement:** read-only against the real world, throwaway databases, **no fixes** (a reviewer who patches stops looking), and reproduce-or-discard. Verified negatives were required too.

### D-20 · The reviews found real, disqualifying defects
Logged here because the fact that the build *self-reported success* while carrying these is the most important lesson in this document. All were reproduced; the headline ones were independently re-verified before any fix was dispatched.

- **The engine never read the real suppression list.** `sync_from_file()` had zero callers. All 16 addresses that had said "stop" were invisible, while the dashboard reported a healthy suppression count. **The adversarial test that certified this called the unwired function inside its own test body** — it tested the function, not the wiring, and the wiring was the entire defect.
- **`dmctl attrib` booked a demo for any GHL contact that merely string-matched** — no tag, no source, no appointment. With ~1,600 contacts, the fourth column was synthesized from CRM noise.
- **Replies that arrived before the message was sent were counted.** No lower time bound. The operator has cold-emailed this same ICP from this same account since June.
- **The linter passed a live URL, the product name, and `Hey None,` in one message.** L1 was a 27-entry TLD allowlist (`bit.ly` sailed through); the homoglyph fold omitted Greek α; L5 caught unrendered `{first_name}` but not a token that had already rendered.
- **An agent could approve outreach** with a review file it wrote itself, bound to nothing — and `touch` defeated the staleness check, so the audit trail actively misled about what was approved.

### D-21 · Fixes dispatched in three disjoint lanes
Three parallel fixers partitioned by file ownership — copy, gates, measurement. The findings clustered cleanly by module, and disjoint ownership is what let the original build parallelize safely. `cli.py` was assigned to exactly one lane.

### D-22 · Fix the class, not the instance
Three findings shared one root — **the system validated the shape of its evidence but never its provenance**: a reply trusted because it exists rather than because it arrived after a send; a CRM contact trusted because a string matched rather than because it records a booking; a review file trusted because it exists rather than because it binds to the message text.

Two more shared another — **allowlists pretending to be denylists** (27 TLDs, a hand-typed confusables table). Patching instances would have left both classes open.

### D-23 · False positives rated as seriously as false negatives
`drainage solution`, `thanks,\nthe operator`, `- the operator` and a rhetorical hook question were all being wrongly *rejected*.

**Why it matters equally:** a linter that rejects good copy trains its operator to route around it, and what the operator would be routing *into* is the false-negative list.

**Root cause worth remembering:** the build's own 13 "legitimate copy" cases were all middle-paragraph substitutions into one fixed hook and one fixed close. Every false positive found lived in the hook or the sign-off — the two slots the fixture never varied.

### D-24 · CAN-SPAM made possible, not mandatory
L6 (last line must be exactly `the operator`), L8 (the question must close) and L1 (no links) interlocked so that **no CAN-SPAM footer could pass lint**. Nobody decided this; it emerged from three independently reasonable rules.

**Decision:** support a delimited footer block exempt from L6/L8 so a compliant footer *can* ship — not mandatory. Cold B2B email is a commercial message under 15 U.S.C. §7704(a)(5), but a mandatory footer would change every message and cuts against the under-120-words pain-only framework. Making it possible is the reversible choice.

---

## Phase 5 — Repair

### D-25 · All three fixers died mid-edit; there was no git history to revert to
The three fix agents hit a session limit simultaneously and terminated partway through. The repo went from 916 passing to **28 failing**, with half-applied refactors across four modules. The project had not been `git init`ed, so there was no revert.

**Decision:** repair forward by hand. Most of the work had landed; the breakage was concentrated in a few missing definitions. Reverting would have thrown away three CRITICAL fixes to undo two type definitions.

**The lesson, logged because it cost the most:** `git init` should have happened at Phase 0. The plan's own open questions even recommended it, and it never got done. A repo with no history has no undo, and the moment you need one is never a moment you planned for. (Fixed at D-29.)

### D-26 · Distinguishing "broken" from "intentionally changed", test by test
Of the 28 failures, only **two** were genuine breakage: a missing import and a type that was referenced but never defined. **The other 26 were tests asserting the OLD semantics against deliberate, correct new behavior** — a consent withdrawal now covering the company domain, G12 firing ahead of G8, a review file now requiring a batch header and a content hash, the cap now measuring body similarity rather than trusting a flag.

**Decision:** each was verified against the *intent* of the fix before its test was touched. Updating a test to match code is how you certify a bug; the discipline was to confirm the new behavior was the one the finding demanded, then update the test to assert it — and where a change created a new risk, add a test pinning the risk rather than only the change.

**Concrete instance:** the domain-scope propagation that fixes the twin-row hole would be catastrophic applied to consumer mailboxes — one unsubscribe from a gmail.com address would suppress every Gmail-hosted prospect. The freemail guard was verified present and then pinned by a test asserting zero domain rows are written. That test did not exist before and is worth more than the one it accompanies.

### D-27 · A refusal that predates the outreach is not a card
The attribution fix correctly stopped silently discarding unattributable bookings — but routed *every* out-of-window booking to a human-confirm card, including contacts created before any message could have left.

**Decision:** split the two directions. Past the window → a card (it may still be ours). Before the earliest possible send → counted in its own bucket, never carded. With ~1,600 GHL contacts, carding the causally impossible ones would bury the real confirms.

### D-28 · The copy fixes shipped without their tests, so the tests were written afterward
The copy fixer died after editing `lint.py` but before writing regression tests. Direct probing confirmed all four criticals were genuinely fixed.

**Decision:** working-but-unpinned is not done. A `RED-TEAM REGRESSIONS` block was added covering every payload from the report, including the must-stay-clean cases, carrying a comment explaining the two structural traps so a future editor doesn't reintroduce them.

**Why:** the difference between "works today" and "stays working" is entirely whether a test would notice.

### D-29 · Version control, finally
`git init -b main` + first commit `4f8ddfd` (81 files, 22,521 insertions). The staged set was verified before committing: zero `.venv`/`.env`/DB/token/cache files tracked, `.env.example` holding names and no values, and the three secret-scan hits confirmed to be the scanners' own pattern literals plus one deliberately planted `ghp_` in a self-test.

---

## Phase 6 — The DM pivot

### D-30 · "DM" meant WhatsApp and Messenger, not LinkedIn
The operator asked what had been built, then flagged the mismatch: the thing is called a **DM** engine, and the smoke test was running on email.

**What went wrong:** the report said "cold DM **or** email." LinkedIn was inferred from the ICP and the existing `browser-helper` skill, and email became the default channel because it was the only one that produces a verifiable artifact. Neither inference was ever checked with the operator. This is exactly the ambiguity class where different readings lead to materially different work — the case for asking rather than assuming.

**Correction:** WhatsApp and Messenger added as first-class channels.

### D-31 · The platform constraint, stated accurately
An initial framing that Meta "prohibits cold DM" was too broad and the operator pushed back correctly. The accurate version:

- Meta's **Business APIs** forbid cold business-initiated messaging — WhatsApp requires opt-in plus an approved template; Messenger only allows replies inside a 24-hour window.
- **A human messaging from their own account is not an API flow at all.** That is ordinary use of the app.

**Decision:** the engine writes; the operator sends by hand. Which means the existing human-send card pattern was already the right architecture — the new channels are that adapter with different constraints.

**The residual risk is behavioral, not policy:** WhatsApp bans are driven by volume, velocity, near-identical text and low reply rates, and they apply to personal accounts too. Hence D-32.

### D-32 · DM caps far below email, and for a different reason
WhatsApp 8/12 per day, Messenger 12/18, against email's 20/30.

**Why:** the email caps are a *deliverability* rule protecting a reputation that recovers. A banned WhatsApp number does not recover, and it's the number on the vans. The cap is also about as many as one person hand-sends before the copy starts repeating — which is itself the detection signal being avoided. The body-similarity check applies on the DM channels too.

### D-33 · Channel-aware identity, mirrored in SQL
`canonical_address(channel, address)` dispatches: phone digits for WhatsApp, Page handle for Messenger, mailbox rules for email. All three are **reimplemented as generated columns in the migration**, so trigger G8 folds identically and the gate holds against a raw `sqlite3` write. A lockstep test runs both implementations over the same table and asserts zero divergence.

**Decided explicitly:** `domain` scope is **meaningless** on a phone number, so it raises rather than silently doing nothing. Company-wide reach isn't lost — the prospect still goes SUPPRESSED and G12 blocks every channel. Conversely, `person` scope now crosses **all** channels: a hard-no on WhatsApp suppresses that human on Messenger and email too.

---

## Phase 7 — What running it for real turned up

Both bugs below were found by driving the DM pipeline end to end **after** the adversarial review, on a fully green suite. Neither would have been caught by any test that existed.

### D-34 · Messenger canonicalization kept `#` and `?`
The operator's own profile URL ends in `#`. It canonicalized to `operatorandrew.chua#`, while the bare URL canonicalized to `operatorandrew.chua` — **two different identities for one person.** A hard-no recorded against one spelling would silently fail to block the other, and a scraper handing back `?ref=...` would sail past it.

Fixed in both the Python and the SQL generated column. The variants were folded into the existing `PAGE_SPELLINGS` fixture, which feeds both the suppression test and the lockstep test, so one edit strengthened both.

**Same failure class as the plus-addressing bug the red team found** — which is the point: the class was closed for email and phones, and reopened by a new channel nobody had exercised.

### D-35 · Assignment ignored channel reachability
`assign_batch` selected candidates by market alone. A WhatsApp batch therefore claimed a Messenger-only prospect, which was then blocked at compose for a missing contact — and because an assignment is one-per-experiment, that prospect could **never** join the Messenger batch that could actually reach it. It silently costs a prospect.

**Why it mattered immediately:** with DM-first outreach most contractors have a phone *or* a Page, rarely both. This would have bitten on the first real batch.

**Fix:** the candidate query now requires a contact on the batch's channel. The test fixtures that broke were creating prospects with **no contacts at all** — unrealistic, since such a prospect cannot be messaged on any channel. The fixtures were corrected rather than the filter relaxed.

### D-36 · The approval gate was not bypassed, on either smoke test
Both smoke tests stopped at the human gate rather than self-approving.

**Why:** an agent writing a review file and then approving it is *precisely* the attack the review found and fixed (D-20). Being the first thing to route around a freshly-installed gate would have made the smoke test meaningless — it would have tested everything except the guarantee that matters most.

### D-37 · The email smoke test was deleted rather than kept
The operator has no email sending infrastructure and hand-sends anyway, so email volume was never the plan. The five staged email drafts and their scratch database were deleted rather than left as ambiguous half-state, and the smoke test re-run on the DM path.

---

## Where it stands

`1074 passed, 0 failed`. Every CRITICAL and HIGH from the adversarial review is fixed and pinned by a regression test using the reviewer's exact attack input.

**The system has never been pointed at a real prospect.** On the evidence of this log, it should not be on the strength of a green suite alone: 916 tests passed while the engine could not read the suppression list, credited bookings to companies it never messaged, and let a shortener through in a first-touch email — and two more real bugs surfaced after that, the first time anyone actually ran it.

A DM smoke test is staged and waiting at the approval gate.
