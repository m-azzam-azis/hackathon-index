#!/usr/bin/env bash
# One command for everything:
#   ./run.sh            pull latest code, then crawl all stages (resumes where it stopped)
#   ./run.sh status     just print what's collected so far
#   ./run.sh --limit 10 any crawl option passes through (see README)
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only -q 2>/dev/null || echo "(couldn't pull latest code; running what's here)"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -e . >/dev/null

if [ "${1:-}" = "status" ]; then
  exec python -m hackathon_index status
fi

# Keep the Mac awake while crawling (no-op elsewhere).
KEEP_AWAKE=""
command -v caffeinate >/dev/null && KEEP_AWAKE="caffeinate -i"
exec $KEEP_AWAKE python -m hackathon_index crawl all "$@"
