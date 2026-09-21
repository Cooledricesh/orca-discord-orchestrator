#!/bin/zsh
# 상시 세션 감시. 5분마다 launchd 가 실행. 죽은 상담역/접수원을 Orca 터미널로 다시 띄우고, 켜 둔 비전이 죽어 있으면 직전 세션을 resume 한다.
# leads-check.sh [--restart <역할>]   --restart 면 살아 있어도 종료 후 재기동 (컨텍스트 비우기).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
restart=""; [[ "${1:-}" == "--restart" ]] && restart="${2:-}"
if [[ -n "$restart" ]] && ! role_enabled "$restart"; then print -- "$restart: 비활성"; exit 0; fi
if ! role_enabled 상담역 && ! role_enabled 접수원 && ! role_enabled 비전; then
  print -- "감시할 상시 역할 없음"; exit 0
fi
ensure_state
alive() { pid_is "$(cat "$STATE_DIR_ROOT/leads/$1.pid" 2>/dev/null || true)" claude; }

# Orca 가 없으면 아무것도 못 한다. 하루 한 번만 #운영-로그에 알린다 (밤새 죽어 있어도 아침에 알 수 있게).
if ! orca_ok; then
  marker="$STATE_DIR_ROOT/orca-down.notified"; today="$(date +%Y-%m-%d)"
  if [[ "$(cat "$marker" 2>/dev/null || true)" != "$today" ]]; then
    ops="$(route_get '["opsLogChannelId"]' 2>/dev/null || route_get '["generalChannelId"]')"
    discord_api "$(ops_bot)" POST "/channels/$ops/messages" "$(python3 -c 'import json,sys;print(json.dumps({"content":sys.argv[1]}))' "⚠️ Orca 런타임 응답 없음 — 상시 세션·작업자를 띄울 수 없습니다 ($(date +%H:%M)). Orca 앱을 확인해 주세요.")" >/dev/null 2>&1 && print -- "$today" > "$marker"
  fi
  log "Orca 런타임 없음 — 건너뜀"; exit 0
fi
for role in 상담역 접수원; do
  role_enabled "$role" || continue
  if [[ "$restart" == "$role" ]] && alive "$role"; then
    p="$(cat "$STATE_DIR_ROOT/leads/$role.pid")"; log "재시작: $role (pid $p)"
    kill -TERM "$p" 2>/dev/null || true; sleep 5; pid_is "$p" claude && kill -KILL "$p" 2>/dev/null || true; sleep 2
  fi
  if alive "$role"; then continue; fi
  # 죽은(또는 방금 죽인) 세션의 옛 Orca 탭을 닫는다 — 자동 복구에서도 탭이 쌓이지 않게
  h="$(cat "$STATE_DIR_ROOT/leads/$role.term" 2>/dev/null || true)"; [[ -n "$h" ]] && orca terminal close --terminal "$h" --tab --json >/dev/null 2>&1 || true
  log "기동: $role"; "$ORCH_ROOT/bin/lead-up.sh" "$role" || log "기동 실패: $role"
done
# 플러그인 자식 확인: 살아 있는 상시 세션·마크에 Discord 플러그인이 없으면 #운영-로그에 한 번만 ⚠️ (상태가 바뀔 때만; 회복되면 표시 해제)
pm_dir="$STATE_DIR_ROOT/plugin-missing"; mkdir -p "$pm_dir"
check_plugin() {  # <키> <표시> <pid>
  local key="$1" label="$2" pid="$3" marker="$pm_dir/$1"
  if plugin_child "$pid"; then rm -f "$marker"
  elif [[ ! -f "$marker" ]]; then
    ops_log "$(ops_bot)" "⚠️ $label 세션은 살아 있지만 Discord 플러그인이 없음 — Discord 를 못 듣습니다. 관리 세션에서 확인 필요"
    touch "$marker"; log "플러그인 없음: $label"
  fi
}
for role in 상담역 접수원; do
  role_enabled "$role" || continue
  if alive "$role"; then check_plugin "$role" "$(route_get "[\"bots\"][\"$role\"]") ($role)" "$(cat "$STATE_DIR_ROOT/leads/$role.pid")"; fi
done
registry_list active | while IFS=$'\t' read -r tid st project bot handle started; do
  [[ -n "$tid" ]] || continue
  p="$(cat "$THREADS_DIR/$tid.pid" 2>/dev/null || true)"; pid_is "$p" claude || continue   # 죽은 터미널은 sweep 담당
  check_plugin "worker-$tid" "$bot ($project, 스레드 $tid)" "$p"
done
find "$pm_dir" -name 'worker-*' -mtime +1 -delete 2>/dev/null || true

# 비전: 등록부가 active 인데(사용자가 stop 하지 않았는데) 백엔드가 없으면 직전 세션 복원 (재부팅·크래시)
vreg="$STATE_DIR_ROOT/vision.json"
if role_enabled 비전 && [[ -f "$vreg" && "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$vreg")" == active ]] \
   && ! bun "$ORCH_ROOT/codex-worker/control.ts" health "$vreg" >/dev/null 2>&1; then
  log "비전 백엔드 없음 → resume"
  "$ORCH_ROOT/bin/vision.sh" resume || log "비전 resume 실패"
fi
