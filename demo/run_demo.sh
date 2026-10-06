#!/usr/bin/env bash
# End-to-end demo of the outbound pipeline on SYNTHETIC data. Nothing is sent anywhere:
# the engine has no send code, and this demo uses the LinkedIn channel, whose "staging"
# step only writes a copy-ready card into data/demo-out/. All names are fictional.
#
#   ./demo/run_demo.sh
#
# The "human" steps (approve / reject / mark-sent / log-reply) are done through the
# engine's non-TTY lane with an explicit actor, to stand in for the person who would
# normally do them at a terminal or in a dashboard.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DM_ENGINE_DB="$PWD/data/demo.db"
export DM_ENGINE_DASHBOARD_METRICS_DIR="$PWD/data/demo-out/metrics"
export DM_ENGINE_DASHBOARD_APPROVALS_DIR="$PWD/data/demo-out/approvals"
BATCH="B-DEMO-1"
ACTOR="demo-reviewer"

# start clean so the demo is repeatable
rm -f "$DM_ENGINE_DB" "$DM_ENGINE_DB"-wal "$DM_ENGINE_DB"-shm
rm -rf "$PWD/data/demo-out" "$PWD/data/review/$BATCH"-* "$PWD/data/outbox"

d() {  # run dmctl, hide two environment warnings that only matter on a real install
  local out rc
  out="$(./dmctl "$@" 2>&1)" && rc=0 || rc=$?
  printf '%s\n' "$out" | grep -v -e 'no shared suppression list' -e 'cap ledger is' || true
  return "$rc"
}
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. create the database and load the five message variants"
d init

step "2. import 12 fictional prospects and qualify them"
d import-prospects demo/prospects.json --market "Demo Valley"
for id in $(seq 1 12); do d qualify "$id" >/dev/null; done

step "3. open a batch and assign variants deterministically (re-running gives identical output)"
d new-batch "$BATCH" --experiment EXP-2026-08-skeptic --market "Demo Valley" --channel linkedin
d assign --experiment EXP-2026-08-skeptic --batch "$BATCH"
d compose --batch "$BATCH" >/dev/null

step "4. the lint gate: 12 drafts in, 3 are rejected on purpose (a link, a product name, an emoji)"
# both exit non-zero on purpose when drafts are rejected, so tolerate that here
d lint-check "data/review/$BATCH-composejob.json" demo/drafts.json || true
d ingest-drafts demo/drafts.json || true

step "5. write the human review file, then approve 8 messages and reject 1"
d review --batch "$BATCH" >/dev/null
for id in 1 2 3 4 5 6 7 8; do d decide --batch "$BATCH" "$id" --choice approve --actor "$ACTOR" | head -1; done
d decide --batch "$BATCH" 9 --choice reject --reason "too long for a DM" --actor "$ACTOR" | head -1

step "6. stage the approved messages (writes copy-ready cards; it cannot send)"
d stage --batch "$BATCH"

step "7. record what the human did: six messages were sent by hand"
for id in 1 2 3 4 5 6; do d mark-sent "$id" --evidence "demo: simulated manual send"; done

step "8. record two replies, then read the measurement sheet"
d log-reply 2 --channel linkedin --class question --snippet "what does it cost?" --actor "$ACTOR"
d log-reply 6 --channel linkedin --class no --snippet "not interested, thanks" --actor "$ACTOR"
d sheet
d stats

step "9. pipeline status (note the honesty warnings: n is far too small to conclude anything)"
d status

printf '\nDone. Cards: data/demo-out/approvals/   Review file: data/review/%s-review.md\n' "$BATCH"
