#!/bin/zsh
# 마크 종료. finish-worker.sh <threadId> <succeeded|failed|stopped>
# 스레드 이름·보관 → 등록부 → 풀 반납 → STATE_DIR 삭제 → 대기열 스폰 → 터미널 close / PID kill
# (작업자가 자기 finish 를 부르면 터미널 close 에서 자기 프로세스가 죽는다. 그 전에 Discord 답장을 끝내 둔다.)
set -euo pipefail
source "$(dirname "$0")/lib.sh"

thread="${1:-}"; outcome="${2:-}"
[[ "$thread" == <-> ]] || die "usage: finish-worker.sh <threadId> <succeeded|failed|stopped>"
case "$outcome" in succeeded) st=done;; failed) st=failed;; stopped) st=stopped;; *) die "outcome 은 succeeded|failed|stopped";; esac
[[ -f "$(registry_file "$thread")" ]] || die "등록부 없음: $thread"
case "$(registry_get "$thread" status)" in done|failed|stopped) print -- "already finished thread=$thread"; exit 0;; esac
with_thread_lock "$thread" "$ORCH_ROOT/bin/finish-worker.sh" "$thread" "$outcome"
case "$(registry_get "$thread" status)" in done|failed|stopped) exit 0;; esac

pidf="$THREADS_DIR/$thread.pid"; pid="$(cat "$pidf" 2>/dev/null || true)"
bot="$(registry_get "$thread" bot)"
handle="$(registry_get "$thread" terminalHandle)"
prompt="$(registry_get "$thread" promptFile)"
project="$(registry_get "$thread" project)"
sd="$(state_root)/workers/$thread"

case "$st" in done) prefix="✅";; failed) prefix="❌";; *) prefix="⏹";; esac
tname="$(discord_api "$bot" GET "/channels/$thread" 2>/dev/null | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("name",""))
except Exception: print("")')" || tname=""
if [[ -n "$tname" ]]; then
  tname="${tname#🔧 }"; tname="${tname#✅ }"; tname="${tname#⏹ }"; tname="${tname#❌ }"
  discord_api "$bot" PATCH "/channels/$thread" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1][:100]}))' "$prefix $tname")" >/dev/null 2>&1 || true
fi
[[ -n "$bot" ]] && discord_archive_thread "$bot" "$thread"

# 터미널을 먼저 닫으면 자기 세션에서 호출한 정리 스크립트도 종료될 수 있다.
# 등록부·풀·로그 정리를 끝낸 뒤 맨 마지막에 터미널을 닫는다.
rm -f "$pidf" "$THREADS_DIR/$thread.activity" "$THREADS_DIR/$thread.progress.json" "$THREADS_DIR/$thread.progress.lock"
registry_update "$thread" "status=$st" "endedAt=$(now)"
"$ORCH_ROOT/bin/pool.sh" release "$thread" >/dev/null || log "pool release 실패"
[[ -d "$sd" ]] && rm -rf "$sd"
[[ -n "$prompt" && -f "$prompt" ]] && rm -f "$prompt"
[[ -n "$project" ]] && { mkdir -p "$ORCH_ROOT/runs/$project"; print -- "- $(date +%H:%M) $st $bot thread=$thread" >> "$ORCH_ROOT/runs/$project/$(date +%Y-%m-%d).md"; }

# 대기열: 봇이 반납됐으니 queued 하나를 스폰한다. 이 프로세스가 곧 죽어도 살아남도록 nohup + disown.
q="$(registry_list queued | head -1 | cut -f1 || true)"
if [[ -n "$q" ]]; then
  log "대기열 스폰: $q"
  nohup "$ORCH_ROOT/bin/retry-queued.sh" "$q" >> "$STATE_DIR_ROOT/log/queue-spawn.log" 2>&1 &!
fi
[[ -n "$bot" ]] && ops_log "$bot" "$prefix $(route_get "[\"botDisplay\"][\"$bot\"]" 2>/dev/null || print -- "$bot") 종료 ($st) — $project · $(registry_get "$thread" taskTitle)"
print -- "finished thread=$thread status=$st bot=${bot:-?}"

# 프로세스 정리 (재부팅 후 재사용된 PID 는 건드리지 않는다).
[[ -z "$handle" ]] || orca terminal close --terminal "$handle" --tab --json >/dev/null 2>&1 || true
if pid_is "$pid" claude; then
  sleep 3; kill -TERM "$pid" 2>/dev/null || true; sleep 3; pid_is "$pid" claude && kill -KILL "$pid" 2>/dev/null || true
fi
