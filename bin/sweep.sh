#!/bin/zsh
# 마크 정리 (launchd 5분): 터미널이 사라진 active → failed, IDLE_MIN(30분) 동안 사용자 메시지·터미널 출력이 없으면 → stopped, 고아 lease 회수.
# 무응답 기준: max(<threads>/<tid>.activity mtime (플러그인이 메시지 수신 시 갱신), Orca 터미널 lastOutputAt). 둘 다 없으면 startedAt.
# 비전은 여기서 다루지 않는다 (bin/vision.sh, 사용자가 직접 종료).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
orca_ok || { log "Orca 런타임 없음 — 건너뜀"; exit 0; }
IDLE_MIN="${IDLE_MIN:-30}"
MAX_AGE_SEC=$((IDLE_MIN*60))
now_epoch=$(date +%s)
to_epoch() { python3 -c 'import sys,datetime;print(int(datetime.datetime.strptime(sys.argv[1],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()))' "$1" 2>/dev/null || echo "$now_epoch"; }

registry_list active | while IFS=$'\t' read -r tid st project bot handle started; do
  [[ -n "$tid" ]] || continue
  # handle 이 비어 있으면 스폰 진행 중 → 터미널 검사는 건너뛰고 무응답 판정만
  term=""
  if [[ -n "$handle" ]] && ! term="$(orca terminal show --terminal "$handle" --json 2>/dev/null)"; then
    log "터미널 없음 → failed: $tid ($bot, $project)"
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
