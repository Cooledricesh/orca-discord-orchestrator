#!/bin/zsh
# 다른 세션(접수원/상담역/운영자)에서 작업자 강제 종료. stop-worker.sh <threadId>
set -euo pipefail
source "$(dirname "$0")/lib.sh"
thread="${1:-}"; [[ -n "$thread" ]] || die "usage: stop-worker.sh <threadId>"
st="$(registry_get "$thread" status)"
[[ -n "$st" ]] || die "등록부 없음: $thread"
if [[ "$st" == "queued" ]]; then registry_update "$thread" status=stopped "endedAt=$(now)"; print -- "dequeued thread=$thread"; exit 0; fi
exec "$ORCH_ROOT/bin/finish-worker.sh" "$thread" stopped
