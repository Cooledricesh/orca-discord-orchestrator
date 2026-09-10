#!/bin/zsh
# retry-queued.sh <threadId> — 등록부에 queued 로 남은 요청을 다시 스폰한다 (요청 원문은 파일로 전달).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
thread="${1:-}"
[[ "$thread" == <-> ]] || die "usage: retry-queued.sh <threadId>"
[[ "$(registry_get "$thread" status)" == queued ]] || die "queued 스레드가 아닙니다: $thread"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/worker-queue.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
registry_get "$thread" taskTitle > "$tmp/title"
registry_get "$thread" currentRequest > "$tmp/request"
qargs=()
for field in model effort; do
  value="$(registry_get "$thread" "$field")"; [[ -z "$value" ]] || qargs+=("--$field" "$value")
done
[[ "$(registry_get "$thread" newWorktree)" == True ]] && qargs+=(--new-worktree)
[[ "$(registry_get "$thread" resume)" == True ]] && qargs+=(--resume)
"$ORCH_ROOT/bin/spawn-worker.sh" "$(registry_get "$thread" channelId)" "$thread" \
  --title "@$tmp/title" --request-file "$tmp/request" --request-message-id "$(registry_get "$thread" requestMessageId)" "${qargs[@]}"
