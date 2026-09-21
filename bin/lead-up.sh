#!/bin/zsh
# 상시 세션 기동. lead-up.sh <상담역|접수원>
# Orca 터미널에 띄운다 (Claude 는 TTY 가 필요해 launchd 가 직접 못 띄운다). pid → state/leads/<역할>.pid, 터미널 핸들 → .term
set -euo pipefail
source "$(dirname "$0")/lib.sh"
role="${1:-}"
[[ "$role" == "상담역" || "$role" == "접수원" ]] || die "usage: lead-up.sh <상담역|접수원>"
role_enabled "$role" || { print -- "$role: 비활성"; exit 0; }

bot="$(route_get "[\"bots\"][\"$role\"]")"
model="$(route_get "[\"models\"][\"$role\"]")"
effort="$(route_get "[\"models\"][\"${role}Effort\"]" 2>/dev/null || true)"
owner="$(owner_id)"; general="$(route_get '["generalChannelId"]')"; plugin="$(channel_args)"
ack="$(route_get '["emojis"]["ack"]')"
sd="$(state_root)/roles/$role"
cwd="$ORCH_ROOT/sessions/$role"
[[ -d "$cwd" ]] || die "세션 cwd 없음: $cwd"

if [[ "$role" == "상담역" ]]; then
  access="$(python3 -c 'import json,sys;print(json.dumps({"dmPolicy":"allowlist","allowFrom":[sys.argv[2]],"groups":{sys.argv[1]:{"requireMention":False,"allowFrom":[sys.argv[2]]}},"ackReaction":sys.argv[3]}))' "$general" "$owner" "$ack")"
  extra_env="DISCORD_PRESENCE='기획 · DM 열려 있음' DISCORD_IGNORE_OTHER_BOT_MENTIONS=1"
  # 검수 브리프의 <@ID> 에 쓸 자비스 앱 ID 를 시스템 프롬프트로 주입 (문서에 리터럴을 두지 않는다)
  jarvis_app_id="$(reviewer_app_id)"
  context="$(runtime_context) 자비스 앱 ID: ${jarvis_app_id:-(없음)}"
else
  # 프로젝트 채널 + #운영-로그 (사용자가 쓰면 status.sh 결과를 올린다. 봇 메시지는 플러그인이 버리므로 자동 기록에는 반응하지 않는다)
  ops_ch="$(route_get '["opsLogChannelId"]' 2>/dev/null || true)"
  access="$( { project_channels; [[ -n "$ops_ch" && "$ops_ch" != null ]] && print -- "$ops_ch"; } | python3 -c 'import json,sys;owner=sys.argv[1];g={c.strip():{"requireMention":False,"allowFrom":[owner]} for c in sys.stdin if c.strip()};print(json.dumps({"dmPolicy":"allowlist","allowFrom":[owner],"groups":g,"ackReaction":sys.argv[2]}))' "$owner" "$ack")"
  extra_env="DISCORD_TOP_LEVEL_ONLY=1 DISCORD_THREAD_REGISTRY=$(shq "$THREADS_DIR") DISCORD_PRESENCE='접수 대기 중' DISCORD_IGNORE_OTHER_BOT_MENTIONS=1"
  context="$(runtime_context)"
fi
extra_args=" --append-system-prompt $(shq "$context")"
[[ -z "$effort" ]] || extra_args+=" --effort $(shq "$effort")"

inner="$(runtime_exports) cd $(shq "$cwd") && print \$\$ > $(shq "$STATE_DIR_ROOT/leads/$role.pid") && DISCORD_STATE_DIR=$(shq "$sd") DISCORD_ACCESS_MODE=static DISCORD_ACTIVITY_FILE=$(shq "$STATE_DIR_ROOT/leads/$role.activity") $extra_env ORCA_ROLE=$role exec $(shq "$CLAUDE_BIN") --dangerously-skip-permissions --model $(shq "$model") --name $(shq "$bot") $plugin --settings $(shq "$ORCH_ROOT/templates/progress-settings.json")$extra_args"
[[ "${DRY:-0}" == 1 ]] && { print -r -- "$inner"; exit 0; }
ensure_state
mkdir -p "$STATE_DIR_ROOT/leads"
if [[ "${ORCA_LEAD_START_LOCK_PARENT:-}" != "$PPID" || -z "${ORCA_LEAD_START_LOCK_FD:-}" || ! "/dev/fd/${ORCA_LEAD_START_LOCK_FD:-none}" -ef "$STATE_DIR_ROOT/leads/$role.start.lock" ]]; then
  exec python3 "$ORCH_ROOT/bin/lead-start-lock.py" "$STATE_DIR_ROOT/leads/$role.start.lock" "$0" "$@"
fi
orca_ok || die "Orca 런타임 응답 없음"
p="$(cat "$STATE_DIR_ROOT/leads/$role.pid" 2>/dev/null || true)"
if pid_is "$p" claude; then
  h="$(cat "$STATE_DIR_ROOT/leads/$role.term" 2>/dev/null || true)"
  terminal_is "$h" "$role" || die "기존 세션의 터미널 신원을 확인할 수 없습니다"
  print -- "$role: 이미 실행 중 (pid $p); 중복 기동하지 않습니다"
  exit 0
fi
mkstate "$sd" "$bot" "$access"
ensure_plugin "$cwd"
out="$(orca terminal create --worktree "path:$ORCH_ROOT" --title "$(lead_title "$role")" --command "$inner" --json)" || die "orca terminal create 실패: $out"
handle="$(json_get "$out" result.terminal.handle)"
print -- "$handle" > "$STATE_DIR_ROOT/leads/$role.term"
if accept_prompts "$handle" 60; then
  if plugin_ready "$(cat "$STATE_DIR_ROOT/leads/$role.pid" 2>/dev/null)" "$handle" 40; then
    ops_log "$bot" "🔄 $bot ($role) 세션 시작 — 새 컨텍스트"
  elif [[ "${LEAD_RETRY:-0}" == 0 ]]; then
    # 플러그인이 안 붙은 세션은 Discord 를 못 듣는다 → 한 번 다시 띄운다
    warn "Discord 플러그인이 붙지 않음 → 재기동: $role"
    kill -TERM "$(cat "$STATE_DIR_ROOT/leads/$role.pid")" 2>/dev/null || true; sleep 3
    orca terminal close --terminal "$handle" --tab --json >/dev/null 2>&1 || true
    LEAD_RETRY=1 exec "$0" "$role"
  else
    ops_log "$bot" "⚠️ $bot ($role) 세션은 떴지만 Discord 플러그인이 붙지 않음 — 수동 확인 필요"
  fi
else
  warn "터미널이 기동 중 종료됨: $handle"
fi
print -- "role=$role bot=$bot terminal=$handle"
