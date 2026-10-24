#!/usr/bin/env bash
# One entry point. Pulls the latest code and sets up .venv on first use, then:
#   ./run.sh                     interactive menu
#   ./run.sh status              any command: status, score, snapshots, restore NAME, crawl STAGE ...
#   ./run.sh --loser-rate 0.15   options alone mean `crawl all` with those options
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only -q 2>/dev/null || echo "(couldn't pull latest code; running what's here)"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -e . >/dev/null

if [ $# -eq 0 ]; then
  if [ -t 0 ]; then exec python -m hackathon_index menu; fi
  set -- crawl all                     # no terminal (cron, scripts): plain update
elif [[ "$1" == -* ]]; then
  set -- crawl all "$@"
fi

# Keep the Mac awake while crawling (no-op elsewhere).
if [ "$1" = "crawl" ] && command -v caffeinate >/dev/null; then
  exec caffeinate -i python -m hackathon_index "$@"
fi
exec python -m hackathon_index "$@"
