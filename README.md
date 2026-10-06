# Outbound Guardrails Engine

A cold-outreach pipeline that is **structurally unable to send a message**. It imports prospects, assigns message variants, lints every draft against 12 mechanical rules, makes a human approve each message, and then stages it for that human to send. The send step is not behind a flag. It does not exist in the code, and a test fails the build if anyone adds one.

I built it for a small home-services AI-receptionist business (LeadFlow Cure), where cold outreach is regulated (TCPA, CAN-SPAM) and one bad batch can get a sending account banned.

## Read this first: how it was built, and what it is not

**Built by Julius Chua, directing Claude Code.** I specify the behavior, review the output and run the tests; the code is AI-written under my direction. I can read and reason about it, but I did not hand-write most of it, and I would not claim to be a software engineer on the strength of this repo. What I can show is the part I do own: deciding what must be impossible, turning that into tests, and checking that the tests actually bite.

What this repo is not:

- **Not a results story.** It has produced no revenue, no reply-rate figures and no client outcomes that I am claiming. The demo below runs on synthetic data and the numbers it prints are made up.
- **Not a prospecting or copywriting tool.** It does not find leads and it does not write the messages. An agent writes the copy against a job spec the engine emits; the engine owns the disciplined middle.
- **Not a sender.** Staging an email means a Gmail *draft*. Staging a LinkedIn, WhatsApp or Messenger message means a copy-ready card. A person sends.

## Why it exists

Cold outreach to skeptical small-business owners fails in two ways: the copy is generic, and the sender can't tell which message worked. The first needs variants written in the buyer's own language. The second needs a measurement sheet (`variant | sent to | replied | booked`) and the discipline not to read conclusions into twenty data points. Both need guardrails that hold when the person using the tool is tired or in a hurry.

## What it does

```
prospects.json           -> import, dedup, suppression check
                                  |
                            qualify -> batch -> deterministic variant assignment
                                  |
                            compose job -> an agent writes copy against the spec
                                  |
                            lint (12 mechanical rules) -> READY or LINT_FAILED
                                  |
                            human review file -> a person approves
                                  |
                            stage -> copy-ready card, or a Gmail draft
                                  |
                       +------------------------------------+
                       |  A person sends. The engine never. |
                       +------------------------------------+
                                  |
                            record sends and replies -> sheet, stats
```

## The guardrails, and the test that proves each one

Each is enforced in code and backed by a test that checks the enforcement actually fires. A guard that exists but never triggers is worse than none, because it manufactures confidence.

| Guarantee | How it is enforced | Where to look |
|---|---|---|
| **Nothing sends without a person.** | No send code exists: an AST scan over `src/` finds none, and a control test injects `drafts().send()` to confirm the scanner would catch it. There is no `send` subcommand. The state machine has no path to SENT that skips APPROVED, and SQL triggers backstop it. | `tests/adversarial/test_gates.py`: `test_a_no_send_code_anywhere_under_src`, `test_a_the_send_detector_actually_catches_injected_send_code`, `test_a_there_is_no_send_subcommand_on_the_cli` |
| **A first message carries no link and no product name.** | Lint rules L1 and L2, plus trigger G6. Text is NFKC-normalised and homoglyph-folded first, so a Cyrillic or fullwidth lookalike still trips the rule. | `src/dm_engine/lint.py`, `tests/test_lint.py` |
| **12 lint rules (L1 to L12)** | Links, product identity, banned phrases, emoji, unresolved placeholders, email shape, DM shape, exactly one closing question, subject uniqueness, usable company name, step-2 demo link, fake urgency. | `RULES` in `lint.py` |
| **Approval is a human act.** | `approve` refuses to run without a terminal. The only non-terminal path takes an explicit actor and goes through the same gate: same content-digest binding, same staleness check, same suppression re-check. | `src/dm_engine/approval.py` |
| **A suppressed person is never contacted again.** | Address-, domain- and person-scoped, fails closed, re-checked at the last moment before staging. Identity folds per channel, so differently formatted phone numbers or profile URLs are one person. | `src/dm_engine/suppression.py` |
| **Variant results are reproducible.** | Assignment is deterministic (`sorted-hash-rr-v1`), records its hashes and aborts on drift. Re-running gives byte-identical output. | `src/dm_engine/variants.py` |
| **Small samples are reported as small.** | Below 30 delivered per variant and 5 human replies, the stats show raw counts and verbatim replies only. No winner is declared and `retire` refuses. | `src/dm_engine/stats.py` |
| **Database-level backstop.** | 25 SQL triggers across the migrations reject illegal state transitions, unlinted messages, suppressed recipients, unverified email addresses and more, even if the Python is bypassed. | `data/migrations/` |

## Try it

Requires Python 3.12 or newer. The engine itself has no dependencies (standard library only); `pytest` is needed to run the tests.

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
make test     # about 1,230 test cases
make demo     # the whole pipeline on 12 fictional prospects
```

`make demo` runs `demo/run_demo.sh`, which imports 12 fictional prospects, assigns variants, ingests 12 drafts, and shows the lint gate rejecting three of them on purpose:

```
LINT_FAILED redwoodpavers.example: L1: scheme: 'https://example.com/missed-calls' — Message 1 carries no links (standing outreach rule); L7: 4 sentences, limit 3
LINT_FAILED summitpaver.example: L2: product name 'brightflow' appears as 'BrightFlow' — Message 1 names no product
LINT_FAILED willowcreekhardscape.example: L4: emoji '🙂' (U+1F642) — no emoji unless the recipient used one first
```

It then approves eight messages and rejects one (standing in for the human reviewer), stages the approved ones as cards under `data/demo-out/`, records six "manual sends" and two replies, and prints the measurement sheet along with the engine's own honesty warning that the sample is far too small to conclude anything. The demo uses the LinkedIn channel, so it needs no Gmail access and sends nothing.

You will see two environment warnings if you run commands by hand outside the demo script (a missing shared suppression list and a cap-ledger path). Both are expected on a machine that is not set up as the original deployment.

## Design decisions

- **Why it cannot send.** The costly failure in cold outreach is a bad batch going out, not a good batch going out slowly. If sending exists in the code, then a bug, a tired operator or an over-eager agent can send. If it does not, those risks are bounded by what a person is willing to paste and click. I treated "the system cannot do the dangerous thing" as the requirement, not "the system is careful when it does it."
- **Why lint before approval.** A human reviewing 20 messages will miss a link or a product name by message twelve. Mechanical rules catch the rule-shaped mistakes for free, so the person spends their attention on the judgment the machine cannot make: does this sound like a real operator writing to another?
- **Why deterministic variant assignment.** An experiment you cannot reproduce is an anecdote. Assignment is a hash with recorded inputs, so a reviewer can recompute any assignment by hand and any drift aborts instead of silently reshuffling.
- **Why honest statistics.** With twenty sends, any "winning variant" is noise. The engine refuses to print a rate as a conclusion below a stated sample size, because the temptation to read one is exactly when you are least likely to resist it.
- **Why a database backstop under the Python.** Application code can be bypassed by a script, a migration or a future change. Triggers make the illegal states unrepresentable at the lowest layer.

## Layout

```
dmctl                       CLI entrypoint (no install step)
Makefile                    make test / make demo
src/dm_engine/              the engine (about 11,000 lines)
  lint.py                   the 12 rules, normalisation, the CAN-SPAM footer exemption
  approval.py state.py      the human gate and the state machine
  variants.py stats.py      deterministic assignment, honest statistics
  prospects.py suppression.py   identity, dedup, do-not-contact
  compose.py                the compose job an agent writes copy against
  replies.py attribution.py report.py   replies, booking attribution, the sheet
  channels/                 email (Gmail drafts) and manual DM cards
data/migrations/            schema, triggers, the measurement view
data/variants/              the five seed message variants (copy guidance)
tests/                      18 test files, including tests/adversarial/
demo/                       synthetic prospects and drafts, plus run_demo.sh
docs/                       the full plan, spec, manual and review notes
```

## Documentation

`docs/` holds the original plan, spec, operator manual, adversarial review and decision timeline. **They were written for a larger private system**, so references to a dashboard, a send-queue, other agents or a VPS describe components that are not in this repository. The `docs/DECISION_TIMELINE.md` is the quickest way to see how each design choice was made and why.

## Known limits

- The email staging path calls a small Gmail helper that lives outside this repo. Two tests skip when it is not installed (`gmail-helper skill not installed on this host`); everything else, including the demo, runs without it.
- Variants in `data/variants/` are copy *guidance* aimed at one narrow audience (hardscape contractors). They are an example of the format, not a general-purpose message library.
- The product-name list in `lint.py` (rule L2) uses a placeholder brand ("BrightFlow") in this public copy, so the repo carries no real business names. Swap in your own.
- It has been used on a small scale by one operator. I have not load-tested it, and I make no claims about throughput.

## License

MIT. See [LICENSE](LICENSE).
