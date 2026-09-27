#!/bin/zsh
# 작업자 봇 풀. lease <threadId> [engine] [bot] | release <threadId> | status
# state/pool.json 을 fcntl.flock(state/pool.lock) 으로 원자적으로 갱신한다.
# lease: 봇 이름 출력 (exit 0), 그 엔진(기본 claude)의 봇이 모두 사용 중이면 exit 3 (다른 엔진 봇이 비어 있어도).
# bot 을 주면 그 봇만 빌린다. 같은 threadId 가 이미 빌린 봇이 있으면 그 봇을 그대로 돌려준다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

cmd="${1:-status}"; tid="${2:-}"; engine="${3:-claude}"; want_bot="${4:-}"
[[ "$cmd" == "status" || -n "$tid" ]] || die "usage: pool.sh lease <threadId> [engine] [bot] | release <threadId> | status"

python3 - "$POOL_FILE" "$POOL_LOCK" "$cmd" "$tid" "$engine" "$want_bot" "$(worker_entries | cut -f1,2)" <<'PY'
import json, sys, os, fcntl, datetime
pool_file, lock_file, cmd, tid, engine, want_bot, entries = sys.argv[1:8]
engines = dict(l.split("\t", 1) for l in entries.splitlines() if l)
bots = list(engines)
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def read_pool():
    try:
        return json.load(open(pool_file))
    except FileNotFoundError:
        return {}

if cmd == "status":
    pool = read_pool()
    busy = lambda b: bool(pool.get(b, {}).get("threadId"))
    head = f"{sum(map(busy, bots))}/{len(bots)} 사용 중"
    kinds = list(dict.fromkeys(engines.values()))
    if len(kinds) > 1:
        head += " (" + " · ".join(f"{k} {sum(busy(b) for b in bots if engines[b] == k)}/{sum(engines[b] == k for b in bots)}" for k in kinds) + ")"
    print(head)
    for b in bots:
        e = pool.get(b, {})
        print(f"  {b}\t{engines[b]}\t{e.get('threadId') or '-'}\t{e.get('since') or ''}")
    sys.exit(0)

os.makedirs(os.path.dirname(pool_file), exist_ok=True)
with open(lock_file, "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    pool = read_pool()
    for b in bots:
        pool.setdefault(b, {"threadId": None, "since": None})
    # Preserve retired active leases until explicitly released; never reallocate them.
    rc = 0
    if cmd == "lease":
        cands = [b for b in bots if engines[b] == engine and (not want_bot or b == want_bot)]
        held = [b for b in cands if pool[b]["threadId"] == tid]
        free = [b for b in cands if pool[b]["threadId"] is None]
        if held:
            print(held[0])
        elif free:
            pool[free[0]] = {"threadId": tid, "since": now}
            print(free[0])
        else:
            rc = 3
    elif cmd == "release":
        for b in pool:
            if pool[b]["threadId"] == tid:
                pool[b] = {"threadId": None, "since": None}
                print(b)
    else:
        print("unknown command", file=sys.stderr); rc = 2
    tmp = pool_file + ".tmp"
    open(tmp, "w").write(json.dumps(pool, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, pool_file)
sys.exit(rc)
PY
