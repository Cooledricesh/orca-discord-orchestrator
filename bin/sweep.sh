#!/bin/zsh
# 마크 정리 (launchd 5분): 터미널이 사라진 active → failed, IDLE_MIN(30분) 동안 사용자 메시지·터미널 출력이 없으면 → stopped,
# 끝난 스레드 worktree 에 남은 터미널 닫기, 끝난 worktree 7일 후 자동 삭제·주간 보고, 고아 lease 회수.
# 무응답 기준: max(<threads>/<tid>.activity mtime (플러그인이 메시지 수신 시 갱신), Orca 터미널 lastOutputAt). 둘 다 없으면 startedAt.
# 비전은 여기서 다루지 않는다 (bin/vision.sh, 사용자가 직접 종료).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
orca_ok || { log "Orca 런타임 없음 — 건너뜀"; exit 0; }
IDLE_MIN="${IDLE_MIN:-30}"
MAX_AGE_SEC=$((IDLE_MIN*60))
now_epoch=$(date +%s)
to_epoch() { python3 -c 'import sys,datetime;print(int(datetime.datetime.strptime(sys.argv[1],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()))' "$1" 2>/dev/null || echo "$now_epoch"; }

registry_list active | while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  registry_split "$line"
  tid="${fields[1]-}"; st="${fields[2]-}"; project="${fields[3]-}"; bot="${fields[4]-}"; handle="${fields[5]-}"; started="${fields[6]-}"
  [[ -n "$tid" ]] || continue
  # handle 이 비어 있으면 아직 터미널이 없다. 락이 잡혀 있으면 스폰이 진행 중이다.
  # 락이 없으면 기동 프로세스가 이미 죽은 것이다. 다음 sweep(최대 5분)에 자리를 반납한다.
  if [[ -z "$handle" ]]; then
    if spawn_lock_held "$tid"; then continue; fi
    log "스폰 프로세스 없음 → failed: $tid ($bot, $project)"
    notify_thread "$bot" "$tid" "⚠️ 대기 작업이 시작되지 못했습니다 (${project:-프로젝트 없음}, 스레드 $tid). 기동 프로세스가 터미널을 만들기 전에 끝나 자리를 반납합니다."
    "$ORCH_ROOT/bin/finish-worker.sh" "$tid" failed || true
    continue
  fi
  term=""
  if ! term="$(orca terminal show --terminal "$handle" --json 2>/dev/null)"; then
    log "터미널 없음 → failed: $tid ($bot, $project)"
    notify_thread "$bot" "$tid" "⚠️ 작업자 터미널이 없어 실패로 정리했습니다 (${project:-프로젝트 없음}, 스레드 $tid). 자리를 반납합니다."
    "$ORCH_ROOT/bin/finish-worker.sh" "$tid" failed || true
    continue
  fi
  af="$THREADS_DIR/$tid.activity"
  if [[ -f "$af" ]]; then ref_epoch=$(stat -f %m "$af"); else ref_epoch=$(to_epoch "$started"); fi
  out_ms="$( [[ -n "$term" ]] && json_get "$term" result.terminal.lastOutputAt 2>/dev/null || true )"
  [[ "$out_ms" == <-> ]] && (( out_ms/1000 > ref_epoch )) && ref_epoch=$((out_ms/1000))
  age=$(( now_epoch - ref_epoch ))
  if (( age > MAX_AGE_SEC )); then
    log "${IDLE_MIN}분 무응답 → stopped: $tid ($bot, $project, ${age}s)"
    discord_api "$bot" POST "/channels/$tid/messages" "$(python3 -c 'import json,sys;print(json.dumps({"content":sys.argv[1]}))' "⏹ ${IDLE_MIN}분간 메시지가 없어 작업자 세션을 종료했습니다. 이어서 쓰면 새 작업자가 그 메시지로 시작합니다.")" >/dev/null 2>&1 || true
    "$ORCH_ROOT/bin/finish-worker.sh" "$tid" stopped || true
  fi
done

# 안전망: 등록부가 끝난(done/failed/stopped) 스레드의 task-<threadId>-* worktree 에 남은 터미널을 닫는다 (finish 가 건너뛰어진 경우·과거 잔여분)
python3 "$ORCH_ROOT/bin/worktrees.py" sweep || log "끝난 worktree 터미널 정리 실패"
# worktree 정리 (1시간마다): 끝난 스레드 + 미커밋·미병합·보존 파일 없음 + 종료 후 7일 경과 → orca worktree rm. 7일마다 주간 보고.
python3 "$ORCH_ROOT/bin/worktrees.py" periodic 2>>"$STATE_DIR_ROOT/log/worktrees.log" | while IFS= read -r -d '' m; do ops_log "$(ops_bot)" "$m"; done || log "worktree 정리 실패"

# 고아 lease: pool 에 잡혀 있으나 등록부가 active 가 아닌 스레드
python3 - "$POOL_FILE" "$THREADS_DIR" "$ORCH_ROOT/bin/pool.sh" <<'PY'
import json, sys, pathlib, fcntl, subprocess
try: pool = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
threads = pathlib.Path(sys.argv[2])
for b, e in pool.items():
    tid = e.get("threadId")
    if not tid: continue
    if not isinstance(tid, str) or not tid.isdigit(): continue
    # 검사부터 풀 반환까지 같은 락을 유지한다. 검사 직후 새 스폰이
    # 시작되어 그 lease를 회수하는 경쟁을 막는다.
    threads.mkdir(parents=True, exist_ok=True)
    with (threads / f"{tid}.spawn.lock").open("a+") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: continue
        try: record = json.loads((threads / f"{tid}.json").read_text())
        except FileNotFoundError: record = {}
        except (OSError, ValueError): continue
        if record.get("status") == "active": continue
        result = subprocess.run([sys.argv[3], "release", tid], stdout=subprocess.DEVNULL)
        if result.returncode == 0: print(f"고아 lease 회수: {tid}", file=sys.stderr)
PY
print -- "sweep done $(now)"
