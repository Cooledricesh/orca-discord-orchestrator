#!/bin/zsh
# 비전 (GPT/Codex, 라운지 메인 세션 하나). vision.sh fresh [요청파일] | resume | attach | stop | status
#   fresh  : 새 컨텍스트로 시작 (실행 중이면 먼저 stop 해야 한다)
#   resume : 직전 세션(state/vision.json 의 sessionId)을 같은 폴더에서 복원
#   attach : 실행 중인 백엔드에 Orca 터미널(네이티브 Codex TUI)만 다시 붙인다. TUI 를 닫아도 세션은 산다.
#   stop   : 백엔드·app-server·터미널 정리. 세션 ID 는 남겨 resume 에 쓴다.
# 비전은 풀·sweep·인계 없이 사용자가 직접 켜고 끈다. 승인 없이(approval never, sandbox full) 돈다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cmd="${1:-status}"; request_file="${2:-}"
reg="$STATE_DIR_ROOT/vision.json"; prompt_file="$STATE_DIR_ROOT/vision.prompt.md"
channel="$(lounge_channel)"; [[ -n "$channel" ]] || die "routes.json 에 kind=lounge 채널이 없다"
bot="$(route_get "[\"routes\"][\"$channel\"][\"bot\"]")"
vpath="$(project_path_of "$channel")"
sd="$(state_root)/workers/vision"
control() { bun "$ORCH_ROOT/codex-worker/control.ts" "$1" "$reg"; }
alive() { [[ -f "$reg" ]] && control health >/dev/null 2>&1; }
reg_get() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));v=d.get(sys.argv[2],"");print(v if v is not None else "")' "$reg" "$1" 2>/dev/null || true; }

case "$cmd" in
  status)
    if alive; then print -- "비전: alive session=$(reg_get sessionId) since=$(reg_get startedAt) terminal=$(reg_get terminalHandle)"
    else print -- "비전: down (마지막 세션 $(reg_get sessionId))"; fi ;;
  fresh|resume)
    alive && die "비전이 실행 중이다. 진행 중 작업이 끊겨도 되면 먼저 'vision.sh stop'."
    orca_ok || die "Orca 런타임 응답 없음"
    [[ -f "$reg" ]] && { control stop >/dev/null 2>&1 || die "이전 백엔드 정리 미확인 — 프로세스를 확인하라"; }   # 크래시로 남은 app-server 정리
    sid=""; resumed=false
    if [[ "$cmd" == resume ]]; then sid="$(reg_get sessionId)"; [[ -n "$sid" ]] || die "복원할 세션이 없다 (fresh 로 시작)"; resumed=true; fi
    mkdir -p "$sd"; chmod 700 "$sd"; cp "$(bot_env_file "$bot")" "$sd/.env"; chmod 600 "$sd/.env"
    request=""; [[ -n "$request_file" ]] && request="$(cat "$request_file")"
    cat > "$prompt_file" <<EOF
# 비전 기동 정보
- path: $vpath
- 라운지 chat_id: $channel (하위 스레드도 같은 세션. 답은 입력이 온 chat_id 로)
- mode: $cmd
## 현재 사용자 요청
${request:-(없음)}
EOF
    model="$(route_get '["models"]["비전"]["model"]' 2>/dev/null || true)"; effort="$(route_get '["models"]["비전"]["effort"]' 2>/dev/null || true)"
    python3 - "$reg" "$channel" "$(guild_id)" "$(owner_id)" "$bot" "$vpath" "$prompt_file" "$sd" "$sid" "$resumed" "$model" "$effort" "$(now)" <<'PY'
import json, sys, uuid
a = sys.argv
json.dump(dict(channelId=a[2], guildId=a[3], ownerUserId=a[4], bot=a[5], path=a[6], promptFile=a[7], discordStateDir=a[8],
               sessionId=a[9] or None, resumed=a[10] == 'true', model=a[11], effort=a[12], generation=str(uuid.uuid4()),
               status='active', startedAt=a[13], terminalHandle=''), open(a[1], 'w'), ensure_ascii=False, indent=2)
PY
    sid="$(control start)" || die "비전 기동 실패 — $STATE_DIR_ROOT/vision.codex/bridge.log 확인"
    python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));d["sessionId"]=sys.argv[2];json.dump(d,open(p,"w"),ensure_ascii=False,indent=2)' "$reg" "$sid"
    ops_log "$bot" "🟣 비전 $cmd — 세션 $sid"
    "$0" attach ;;
  attach)
    alive || die "비전이 실행 중이 아니다 (fresh 또는 resume)"
    old="$(reg_get terminalHandle)"; [[ -z "$old" ]] || orca terminal close --terminal "$old" --tab --json >/dev/null 2>&1 || true
    inner="cd $(shq "$vpath") && exec bun $(shq "$ORCH_ROOT/codex-worker/control.ts") attach $(shq "$reg")"
    out="$(orca terminal create --worktree "path:$vpath" --title "비전" --command "$inner" --json)" || die "orca terminal create 실패: $out"
    handle="$(json_get "$out" result.terminal.handle)"
    python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));d["terminalHandle"]=sys.argv[2];json.dump(d,open(p,"w"),ensure_ascii=False,indent=2)' "$reg" "$handle"
    print -- "terminal=$handle" ;;
  stop)
    [[ -f "$reg" ]] || { print -- "비전: 등록부 없음"; exit 0; }
    control stop || die "백엔드 정리 미확인 — 프로세스를 확인하라"
    old="$(reg_get terminalHandle)"; [[ -z "$old" ]] || orca terminal close --terminal "$old" --tab --json >/dev/null 2>&1 || true
    python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));d["status"]="stopped";d["terminalHandle"]="";d["endedAt"]=sys.argv[2];json.dump(d,open(p,"w"),ensure_ascii=False,indent=2)' "$reg" "$(now)"
    ops_log "$bot" "🟣 비전 stop — 세션 $(reg_get sessionId) (resume 가능)"
    print -- "비전: stopped (session $(reg_get sessionId) 은 resume 가능)" ;;
  *) die "usage: vision.sh fresh [요청파일] | resume | attach | stop | status";;
esac
