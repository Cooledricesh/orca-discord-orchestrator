#!/bin/zsh
# 자비스 데몬 종료. pidfile 의 pid 가 jarvis/server.ts 인지 확인한 뒤에만 kill 한다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
pidf="$STATE_DIR_ROOT/jarvis.pid"
[[ -f "$pidf" ]] || { print -- "not running (no pidfile)"; exit 0; }
pid="$(cat "$pidf")"
if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidf"; print -- "stale pidfile removed (pid $pid)"; exit 0; fi
cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
[[ "$cmd" == *jarvis/server.ts* || "$cmd" == *"bun server.ts"* ]] || die "pid $pid 는 자비스가 아님: $cmd"
kill -TERM "$pid" 2>/dev/null || true
for i in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
# launchd KeepAlive 가 그 사이 새 데몬을 띄워 pidfile 을 새로 썼을 수 있다 → 우리가 죽인 pid 일 때만 지운다
[[ "$(cat "$pidf" 2>/dev/null)" == "$pid" ]] && rm -f "$pidf"
print -- "stopped pid=$pid"
