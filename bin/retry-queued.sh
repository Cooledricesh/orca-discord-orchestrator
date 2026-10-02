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
# explicit*=False 면 대기 시점 기본값으로만 넘긴다 (봇 항목 model/effort 가 우선). 필드가 없는 이전 기록은 명시값으로 본다.
for field in model effort; do
  value="$(registry_get "$thread" "$field")"; [[ -n "$value" ]] || continue
  if [[ "$(registry_get "$thread" "explicit${(C)field}")" == False ]]; then qargs+=("--default-$field" "$value"); else qargs+=("--$field" "$value"); fi
done
# 대기 시점의 난이도 판정을 그대로 넘긴다 (재시도에서 다시 분류하지 않는다)
[[ -z "$(registry_get "$thread" routeSource)" ]] || qargs+=(--route-source "$(registry_get "$thread" routeSource)" \
  --route-level "$(registry_get "$thread" routeLevel)" --route-confidence "$(registry_get "$thread" routeConfidence)")
for field in engine bot; do
  value="$(registry_get "$thread" "$field")"; [[ -z "$value" ]] || qargs+=("--$field" "$value")
done
[[ "$(registry_get "$thread" newWorktree)" == True ]] && qargs+=(--new-worktree)
[[ "$(registry_get "$thread" resume)" == True ]] && qargs+=(--resume)
err="$(mktemp)"
rc=0
set +e
"$ORCH_ROOT/bin/spawn-worker.sh" "$(registry_get "$thread" channelId)" "$thread" \
  --title "@$tmp/title" --request-file "$tmp/request" --request-message-id "$(registry_get "$thread" requestMessageId)" "${qargs[@]}" 2>"$err"
rc=$?
set -e
cat "$err" >&2
if (( rc != 0 && rc != 3 )); then
  reason="$(python3 -c '
import sys
lines=[l.strip() for l in open(sys.argv[1], errors="replace") if "ERROR:" in l]
text=lines[-1] if lines else ("exit "+sys.argv[2])
if "ERROR:" in text: text=text.split("ERROR:",1)[1].strip()
print((text or ("exit "+sys.argv[2]))[:180])
' "$err" "$rc")"
  project="$(registry_get "$thread" project)"
  notify_thread "$(registry_get "$thread" bot)" "$thread" "⚠️ 대기열에서 작업을 시작하지 못했습니다 (${project:-프로젝트 없음}, 스레드 $thread). ${reason:-exit $rc}"
fi
rm -f "$err"
exit "$rc"
