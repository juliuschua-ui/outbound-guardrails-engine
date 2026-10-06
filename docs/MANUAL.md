# MANUAL — running the Outbound DM Engine

The operator runbook. If you only read one document, read this one.

Everything runs from the project root:

```bash
cd outbound-guardrails-engine
```

`./dmctl` finds its own Python (`.venv/bin/python`) and sets its own `PYTHONPATH`. There is no install step and no environment to activate.

---

## The one thing to internalize

**The engine cannot send.** It composes, lints, gates, and stages. Staging means: an email lands in your Gmail drafts, or a LinkedIn / WhatsApp / Messenger message lands as a copy-ready card. You send it. That is not a policy you could override in a hurry — there is no send code in this repository at all, and a test fails the build if anyone adds one.

So when you see `staged=5`, nothing has left the building. Five drafts are waiting in Gmail for you.

---

## First run, once

```bash
./dmctl init
```

Applies migrations 001–005 and seeds the five message variants. Safe to re-run; it's idempotent.

```bash
./dmctl status
```

Your pipeline at a glance, plus honesty warnings (e.g. "last reply scan was 3 days ago", "n too small to mean anything yet").

---

## Running a batch — the full loop

### 1. Get prospects

Use the the lead-sourcing agent agent or the `/email-outreach` skill. It produces a JSON file in the import contract shape (company, website, state, owner first name, ICP fit note, hook fact, casual company name, and any of four address arrays — `emails`, `linkedin`, `whatsapp`, `messenger` — each carrying its **source**).

The `source` field on each address matters more than it looks — see step 3.

```bash
./dmctl import-prospects ~/leads/boise-2026-08.json --market "Boise ID"
```

Reports `added / dupes / skips / errors`, and never silently drops a row. If a company arrives twice, you're told which row it duplicates.

### 2. Qualify

`qualify` and `exclude` each take **one** prospect id — loop for a range:

```bash
./dmctl qualify 1                          # NEW -> QUALIFIED
for id in $(seq 1 20); do ./dmctl qualify "$id"; done
./dmctl exclude 6 --reason "franchise chain"
```

Only QUALIFIED prospects reach a compose job. This is your ICP gate: design-build / hardscape / outdoor living, under 20 employees, owner-operated. Not the dominant regional player, not a franchise, not a mow-and-blow maintenance outfit.

### 2b. Set the casual name — compose blocks without it

`company_casual` is what the copy actually calls the company ("Cedar Bend", not "Cedar Bend Outdoor Living, LLC"). **Import does not populate it**, and `compose` refuses any prospect missing it — so the first real run blocked on all 8 batches at once until it was filled in.

```bash
./dmctl set-casual 12 "Cedar Bend"
```

It also has to pass L10: no legal suffix, not ALL CAPS, ≤4 words, no trailing punctuation. Two real batches failed lint on `JNK LANDSCAPING` and `RMS LANDSCAPES` reading as shouting before those were set to mixed case.

If a compose job blocks, read its `blocked` array — it names the prospect and the reason:

```bash
.venv/bin/python -c "import json;print(json.load(open('data/review/<batch>-composejob.json'))['blocked'])"
```

### 3. Verify deliverability — do not skip this

**Email only.** SMTP verification is meaningless for a phone number or a Page, so the DM channels skip this step entirely — jump to step 4. (What protects a DM batch instead is the cap and the suppression sync; see the DM section below.)

Bounces damage `sam.taylor@example.com`'s sender reputation, and reputation is the one asset here you cannot buy back.

**Authoritative runs happen on the VPS.** WSL's IP sits on the Spamhaus PBL, so SMTP checks from your laptop get downgraded to RISK rather than a true verdict.

```bash
# on the VPS (send-host) — runs the checks AND writes the attested envelope:
./dmctl verify --emit verdicts.json

# back here:
./dmctl verify --import verdicts.json
```

Use `--emit`. It is the only thing that produces an *attested* envelope — one that records the tool, host and timestamp, which is what lets a PASS stay a PASS on import.

⚠️ **Do not redirect `verify_emails.py` into a file and import that.** That script prints a human-readable table, not JSON — there is no `--json` flag and no `--help` (it reads `--help` as an address to check). A redirect gives you a text table that `--import` cannot parse at all. If you somehow end up with a bare JSON *list* of verdicts, `--import` will accept it but **degrade every verdict**, because nothing in it says where or when it was produced — a PASS you worked for silently becomes a RISK.

The policy the engine enforces for you:

| Verdict | What happens |
|---|---|
| **PASS** | Drafts normally. |
| **RISK** | Drafts **only** if the address came from a verified source (the company website, or a prior reply). A RISK address that came from a format guess is refused. |
| **FAIL** | Dropped. Dead domain, nonexistent mailbox, or a typo. Fall back to LinkedIn. |

Staging refuses any address with no verdict on file at all.

### 4. Open a batch and assign variants

```bash
./dmctl new-batch B-20260803-boise-1 \
    --experiment EXP-2026-08-skeptic --market "Boise ID" --channel email
./dmctl assign --experiment EXP-2026-08-skeptic --batch B-20260803-boise-1
```

Assignment is deterministic (`sorted-hash-rr-v1`) and records its hashes. Re-running gives byte-identical results; if anything about the batch has been tampered with, it aborts rather than quietly reassigning. That's what makes the eventual per-variant numbers defensible.

The five seed variants, all skeptic-first — written for a buyer who has probably already tried something cheaper and got burned:

| | Variant | Angle |
|---|---|---|
| V1 | `burned-before` | Names the graveyard of things they've already tried, without naming any product |
| V2 | `missed-call-math` | Puts them physically on the job — two hours into a paver base, phone ringing |
| V3 | `off-the-tools` | Names the transition they're already in the middle of |
| V4 | `after-hours` | The clock mismatch: their buyers shop after their own workday ends |
| V5 | `competitor-quietly-wins` | Leads with their proof — review count, years, a named build |

### 5. Compose

```bash
./dmctl compose --batch B-20260803-boise-1
```

Writes a compose job to `data/review/<batch>-composejob.json`. Hand it to the copywriting agent, which returns drafts in the ingest contract shape.

```bash
./dmctl ingest-drafts ~/drafts/boise-1.json
# ready=5 lint_failed=0 missing=0 unknown=0
```

Every draft lands in exactly one bucket. A `LINT_FAILED` message tells you which rule it broke; recompose and re-ingest — it reuses the same message row.

**Self-check before you ingest.** `outreach-specialist` (or anyone) can run the same L1–L12 rules against a candidate drafts file before it ever touches the database:

```bash
./dmctl lint-check data/review/<batch>-composejob.json ~/drafts/boise-1.json
# ready=5 lint_failed=0 missing=0 unknown=0
```

No `data/dm.db` writes, no `runs` row, safe to run any number of times against any draft. It reads the compose job's own `avoid_subjects`, `company_casual` and `channel_constraints` — the same inputs `ingest-drafts` uses — so a clean `lint-check` means `ingest-drafts` will land the same drafts as `ready`.

### 6. Review and approve — the human gate

```bash
./dmctl review --batch B-20260803-boise-1
```

Writes a markdown file with every message in full. Read them. Tick one box per message:

```
- [x] approve
- [ ] reject:
```

Then:

```bash
./dmctl approve --from-review data/review/B-20260803-boise-1-review.md
```

Approving in a terminal directly (`./dmctl approve --batch ...`) works too, but only on a TTY — it refuses to run headless, so no automation can approve on your behalf.

### DASHBOARD review lane — click instead of edit-and-run

If reading N markdown files and running N terminal commands is more friction than you want, DASHBOARD has a **DM** tab (`localhost:4100`) that renders every open review file as clickable cards — approve, reject with a reason, or stage, all without a terminal.

```bash
./dmctl review-json --batch B-20260803-boise-1   # what DASHBOARD reads: the same blocks, as JSON
./dmctl decide --batch B-20260803-boise-1 12 --choice approve --actor dashboard-ui
```

`decide` is `approve`'s sibling for this one purpose, not a weaker copy of it: it calls the exact same `approval.approve_from_review()` gate — same content-digest binding, same staleness check, same suppression re-check. What's different is the proof of a human. `approve` proves one by requiring a TTY; `decide` proves nothing by itself — it's reachable the moment an authenticated request hits DASHBOARD's bearer-token-guarded API, same trust level as every other click in DASHBOARD's EXEC layer (dispatch, send-queue arm). That's why `--actor` has no default on `decide`: whatever calls it has to say who it is, so `state_log` never reads "operator" for a click a script made.

Nothing about what this can *do* changes: APPROVE/REJECT still only move a message toward staging, and STAGE still only ever creates a Gmail draft or an outbox card. Sending is exactly as manual as it always was.

**Reading `decide`'s output.** It approves the one message you named, but it runs `approve_from_review()` over the *whole* file, so it always reports on every row:

```
approved=1 rejected=0 skipped=6
  1: not READY (state=APPROVED) — nothing to approve
  4: no box marked — left for a later pass
```

Those skips are the normal state of a batch you are working through one message at a time — they are not failures, and the exit code ignores them. `decide` exits 0 when *your* message moved and 1 only when it didn't. (It used to exit 1 whenever anything at all was skipped, which meant every click on a multi-message batch looked like an error in DASHBOARD while quietly succeeding. Fixed 2026-08-09.)

In the DM tab, a message you have already decided shows a dimmed `APPROVED ✓` and its button stops responding — that is the card being spent, not a broken button.

### 7. Stage

```bash
./dmctl stage --batch B-20260803-boise-1 --dry-run   # look first
./dmctl stage --batch B-20260803-boise-1             # creates Gmail drafts
```

Staging enforces, in one atomic step per message: the daily cap, suppression (re-checked at the last moment), the verification verdict, Gmail-Sent dedup, and the prospect-local send window. If Gmail fails mid-batch, the message stays APPROVED and the cap slot is released — re-run and it picks up exactly where it stopped, with no duplicate drafts.

**Cadence rules the engine holds you to:**
- Max 10 per send window; **20/day** for template-body batches on email and LinkedIn, 30/day if every body is uniquely written. The DM channels are lower — see the WhatsApp/Messenger section
- Stagger sends 3–5 minutes apart
- Never the same subject twice in a batch (or within 30 days)

### 8. Send — this part is you

Open Gmail. The drafts are there. Send them staggered, or arm them through the DASHBOARD EXEC send-queue.

⚠️ **`scheduledSendTime` does not exist in the Gmail API.** Setting it does not schedule anything — the field is silently ignored and **the message sends immediately**. Use Gmail's native scheduled-send UI, or the EXEC queue. Never trust a script that claims to schedule via the API field.

Prospects are national across four time zones while you're on MST. The engine records the intended prospect-local window (roughly 8am–6pm local) in the review file, the dry-run JSON, and the LinkedIn card — a cold email landing at 5am local is a deliverability problem, not just a rude one.

Then record what you did:

```bash
./dmctl mark-sent 12 13 14 --evidence "sent manually 2026-08-03 11:05 MST"
# or, for email, let Gmail tell you:
./dmctl reconcile
```

---

## LinkedIn

LinkedIn is **research-automated, human-sent**. The engine stages a copy-ready card with the message and the profile URL; you paste and send.

There is no automated-DM path, not behind a flag, not in a subprocess. Automated LinkedIn DMing violates their ToS and the realistic downside is permanent loss of your account — which at this volume is worth far more than the channel.

Research reads (profile and company enrichment via `browser-helper`) stay under ≤25 profile views/day across ≤3 sessions, and hard-stop on any checkpoint.

---

## WhatsApp and Messenger — the DM channels

Same shape as LinkedIn, for harder reasons. Run a batch exactly as above; only the channel changes:

```bash
./dmctl new-batch B-20260803-glendale-wa --market "Glendale AZ" --channel whatsapp
./dmctl assign  --batch B-20260803-glendale-wa
./dmctl compose --batch B-20260803-glendale-wa
./dmctl ingest-drafts ~/drafts/glendale-wa.json
./dmctl review  --batch B-20260803-glendale-wa
./dmctl approve --from-review data/review/B-20260803-glendale-wa-review.md
./dmctl stage   --batch B-20260803-glendale-wa
```

`stage` writes one markdown card per message into `data/outbox/whatsapp/` (or `data/outbox/messenger/`) and one DASHBOARD approvals item. Each card carries the destination, the exact text in a copy-ready fence, the company and variant, the prospect-local send window, and the `dmctl mark-sent` line to run afterwards.

### You are the send mechanism, and that is not a workaround

There is no API here and there never will be. Meta's Business APIs forbid the thing we are doing:

- **WhatsApp Business Platform** — cold, business-initiated messaging is prohibited outright. An outbound conversation needs the recipient's prior opt-in *and* a pre-approved message template. A contractor you found on Google Maps has given neither.
- **Messenger Platform** — a Page may only reply inside a 24-hour window that the *user* opens by messaging first. There is no compliant call for a cold first touch.

So you send by hand, from your own account, exactly as a person using the app. The engine holds no Meta token and asks for none. That also means the second release gate is inherent: nothing can go out while you are asleep.

### What actually gets you banned

Not policy — **detection**. WhatsApp reads volume, velocity, near-identical text and a low reply rate, and what it takes is the number itself: the one on your vans, your invoices, and your Google listing. Messenger's version is quieter — Page rate limiting and the "message requests" folder — but the effect on a batch is the same.

The engine holds you to lower caps than email for exactly that reason:

| | Template body | Uniquely written |
|---|---|---|
| WhatsApp | 8/day | 12/day |
| Messenger | 12/day | 18/day |
| Email, LinkedIn | 20/day | 30/day |

And the body-similarity check applies here too: a batch of near-identical DMs takes the template cap however the batch declared itself. That is the rule doing its job — near-identical text is the single loudest signal the detector reads.

### Rules of the channel

- **Message business numbers and company Pages. Not personal mobiles, not personal profiles.** The number on a company's website is a published channel for exactly this. A mobile scraped from somewhere else reads as a stranger who has your number, and a report costs the recipient one tap.
- **Keep the pacing human.** The card prints a staggered release time; use it. Eight messages fired in ninety seconds is a bot however they were typed.
- **Same copy rules, tighter shape.** No links, no product name, one soft question (L1/L2/L8 are unchanged). WhatsApp caps at 3 sentences / 400 characters, Messenger at 4 / 500 — a DM that needs a "Read more" fold has already been read as marketing. There is no subject line and no `the operator` sign-off; the email long-form rule (L6) does not apply.
- **Record what you did.** `./dmctl mark-sent 12 --evidence "sent manually 2026-08-03 11:05 MST"`. `reconcile` is a no-op on these channels by design — a hand-sent DM leaves no trace the engine can read — so replies come in through `./dmctl record-reply`. That command needs a real terminal when you classify a reply as human — recording a human reply is what unlocks Message 2 and its demo link, so it is deliberately not something a script or an agent can do for you.

### Suppression works across channels

A hard-no is a fact about a person, not about an app. `./dmctl suppress <number> --channel whatsapp --reason hard_no` also moves the whole prospect to SUPPRESSED, which blocks their email and their Page too — and punctuation is not a way around it: `+1 (602) 555-0134`, `602-555-0134` and `16025550134` are one entry. Same for a Page: `m.me/x`, `facebook.com/x`, `fb.me/x`, and the spellings a browser or a scraper actually hands you — `facebook.com/x#` and `facebook.com/x?ref=bookmarks` — are all one destination. (That last pair was a real bug, found the first time a profile URL was pasted in from a browser address bar rather than typed.)

`--scope domain` is refused on these channels. A phone number has no domain, and a Messenger handle's "domain" is facebook.com — suppressing that would mute every Page-reachable prospect you have.

---

## Measuring — the part that was the whole point

```bash
./dmctl scan-replies                  # read-only Gmail scan, classifies each reply
./dmctl attrib --ghl-export ghl.json  # match bookings back to messages
./dmctl sheet --csv out.csv           # variant | sent_to | replied | booked
./dmctl stats --replies               # per-variant counts + verbatim replies
./dmctl metrics-emit                  # snapshot for the DASHBOARD dashboard
```

**What counts as a reply:** a human wrote back. An out-of-office, a "we received your message" autoresponder, a delivery-delay notice, and a bounce are none of them — they're classified separately and kept out of the numerator. A bounce additionally suppresses the address.

**Reply rate is human replies ÷ delivered**, where delivered = sent − bounced. Not ÷ sent, not ÷ prospects imported.

**Bookings** are attributed down a confidence ladder — UTM `utm_content` on the Message-2 link → domain match → name+phone → a manual-confirm card. The demo form has no email field, so there is no perfect join; anything below high confidence becomes a card for you to confirm rather than a silent booking. A fabricated attribution would make a variant look like a winner on evidence that doesn't exist.

### Reading the numbers honestly

Below **30 delivered per variant and 5 human replies total**, `stats` prints raw counts and the verbatim replies, and a banner saying the sample is too small to mean anything. No winner. No leader. No rate presented as a conclusion. `./dmctl variants retire` will refuse.

That refusal is the feature. At the 20 attempts the outbound-strategy report asks for, the honest deliverable is *what people actually wrote back*, not a percentage. Read the replies — that's the signal at this n.

Above the gate you get Wilson score intervals, and a variant is only flagged as leading if its lower bound clears **every** rival's upper bound.

---

## Replies: what to do

| They said | You do |
|---|---|
| Hot / curious | Message 2 — warm acknowledgment + the demo link |
| A question | Answer it, then bridge to the demo |
| Soft objection | Reframe. Do not pitch. |
| Hard no / unsubscribe | Graceful exit, then `./dmctl suppress <address> --channel <channel> --reason unsubscribe` — permanent, and across every channel |

Message 2 is gated: the engine refuses to stage one for a prospect who never replied, and an autoresponder does not unlock it.

---

## Exit codes

Worth knowing if you wrap any of this in a script.

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Partial — some rows succeeded, some didn't. Read the output. |
| 2 | Refused. A gate said no (non-TTY approve, an unsupported channel, a config guard). |
| 3 | Gate abort. A database trigger or an illegal state transition stopped you. |
| 4 | Auth. Gmail token expired — re-run `gmail_multi_auth.py --account operator_personal`. Headless runs cannot re-auth, so this also lands in the pending-actions ledger. |

---

## Where things live

| | |
|---|---|
| Database | `data/dm.db` (override with `DM_ENGINE_DB`) |
| Compose jobs + review files | `data/review/` |
| Dry-run output, human-send cards | `data/outbox/{linkedin,whatsapp,messenger}/` |
| Sending account | `sam.taylor@example.com` (personal — better cold deliverability) |
| Tokens | `~/.config/skills/gmail-helper/gmail_token.json` |
| Suppression list (shared) | `~/.config/skills/gmail-helper/suppression_list.txt` |

**Host split:** real sends run on the VPS `send-host`. WSL is draft-only. Both machines hold live refresh tokens, so if the host isn't pinned the cadence rules get violated silently — hence `_sender_host_guard.py`, which makes every `send_*.py` exit non-zero off-host.

Rehearsing safely: point `DM_ENGINE_DB`, `DM_ENGINE_DASHBOARD_METRICS_DIR`, and `DM_ENGINE_DASHBOARD_APPROVALS_DIR` at a temp directory and nothing you do touches live data.

---

## When something goes wrong

| Symptom | What it means |
|---|---|
| `stage` says *no verification verdict recorded* | Step 3 hasn't run for that address. Working as designed. |
| `APPROVED -> SENT is not a legal transition` | You dry-ran, which deliberately changes no state. Stage for real first. |
| `ingest-drafts` reports `missing` or `unknown` | A `prospect_key` in the drafts file doesn't match the batch. Nothing was dropped — fix the key and re-ingest. |
| Assignment aborts on drift | Someone or something changed a stored assignment. Do not force past it; find out what changed. |
| `stats` won't name a winner | The min-n gate. Read the verbatim replies instead. |
