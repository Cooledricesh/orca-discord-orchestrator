#!/bin/zsh
# 봇 상태 한 화면. 사용자가 #운영-로그에 쓰면 접수원이 실행해 결과를 올린다. 관리 세션에서도 그냥 실행하면 된다.
# ✅ 정상 / ⚠️ 세션은 있으나 Discord 플러그인 없음 / ❌ 죽음 / ⏸ 없음(정상적으로 비어 있음)
set -uo pipefail
source "$(dirname "$0")/lib.sh"
line() { print -- "$1 $2" }
for role in 상담역 접수원; do
  bot="$(route_get "[\"bots\"][\"$role\"]")"; p="$(cat "$ORCH_ROOT/state/leads/$role.pid" 2>/dev/null || true)"
  if ! pid_is "$p" claude; then line ❌ "$bot ($role) 죽음 — leads-check 가 5분 안에 재기동"
  elif plugin_child "$p"; then line ✅ "$bot ($role)"
  else line ⚠️ "$bot ($role) 세션은 있으나 Discord 플러그인 없음"; fi
done
n=0
registry_list active | while IFS=$'\t' read -r tid st project bot handle started; do
  [[ -n "$tid" ]] || continue
  p="$(cat "$THREADS_DIR/$tid.pid" 2>/dev/null || true)"; title="$(registry_get "$tid" taskTitle)"
  if ! pid_is "$p" claude; then line ❌ "$bot — $project · $title (프로세스 없음, sweep 이 정리)"
  elif plugin_child "$p"; then line ✅ "$bot — $project · $title"
  else line ⚠️ "$bot — $project · $title (Discord 플러그인 없음)"; fi
done
[[ "$(registry_list active | grep -c .)" == 0 ]] && line ⏸ "마크: 활성 작업 없음"
line "📦" "$("$ORCH_ROOT/bin/pool.sh" status 2>/dev/null | head -1)"
q="$(registry_list queued | grep -c . || true)"; (( q > 0 )) && line ⏳ "대기열 $q 건"
v="$("$ORCH_ROOT/bin/vision.sh" status 2>/dev/null || print -- '비전: 상태 확인 실패')"
case "$v" in *alive*) line ✅ "$v";; *down*) line ⏸ "$v";; *) line ⚠️ "$v";; esac
if launchctl print "gui/$(id -u)/ai.orca.jarvis" 2>/dev/null | grep -q 'state = running'; then line ✅ "자비스 (launchd)"; else line ❌ "자비스 launchd 미실행"; fi
print -- "($(date +%H:%M) 기준)"
