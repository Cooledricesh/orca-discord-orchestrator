#!/bin/zsh
# 자비스(검수 데몬) 기동. jarvis-up.sh [--fg]
#   --fg : 현재 프로세스에서 exec (launchd 용). 아니면 nohup 백그라운드. pidfile 은 데몬 자신이 state/jarvis.pid 에 쓴다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
fg=0; [[ "${1:-}" == "--fg" ]] && fg=1
role_enabled 리뷰어 || { print -- "자비스: 비활성"; exit 0; }
export PATH="$HOME/.bun/bin:$PATH"
command -v bun >/dev/null || die "bun 없음 (~/.bun/bin)"
command -v "$CODEX_BIN" >/dev/null || die "Codex 실행 파일 없음: $CODEX_BIN"
bot="$(route_get '["bots"]["리뷰어"]' 2>/dev/null || print -- 자비스)"
[[ -f "$(bot_env_file "$bot")" ]] || die "봇 env 없음: $(bot_env_file "$bot") — Discord 앱을 만들고 DISCORD_BOT_TOKEN/DISCORD_APP_ID 를 넣을 것"
[[ -f "$ORCH_CODEX_AUTH_FILE" ]] || die "Codex 인증 파일 없음: $ORCH_CODEX_AUTH_FILE — codex login 또는 codexAuthFile 설정"
ensure_state
pidf="$STATE_DIR_ROOT/jarvis.pid"
if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
  print -- "already running pid=$(cat "$pidf")"; exit 0
fi
cd "$ORCH_ROOT/jarvis"
[[ -d node_modules ]] || bun install --no-summary
if (( fg )); then exec bun "$ORCH_ROOT/jarvis/server.ts"; fi
nohup bun "$ORCH_ROOT/jarvis/server.ts" >> "$STATE_DIR_ROOT/log/jarvis.out" 2>&1 &
sleep 2
if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then print -- "started pid=$(cat "$pidf")"; else die "기동 실패 — tail $STATE_DIR_ROOT/log/jarvis.out (기동 오류) / jarvis.log (런타임)"; fi
