#!/bin/zsh
# Run from the candidate checkout, without contacting Discord or starting bots.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests
for d in codex-worker jarvis plugin/discord-orca; do
  (cd "$d"; bun install --ignore-scripts; if [[ "$d" != plugin/discord-orca ]]; then bun run typecheck; fi; bun test)
done
for f in bin/*.sh lead.sh; do zsh -n "$f"; done
