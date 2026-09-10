#!/bin/zsh
# 작업자 봇 풀. lease <threadId> | release <threadId> | status
# state/pool.json 을 fcntl.flock(state/pool.lock) 으로 원자적으로 갱신한다.
# lease: 봇 이름 출력 (exit 0), 풀이 꽉 차면 exit 3. 같은 threadId 가 이미 빌린 봇이 있으면 그 봇을 그대로 돌려준다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

cmd="${1:-status}"; tid="${2:-}"
[[ "$cmd" == "status" || -n "$tid" ]] || die "usage: pool.sh lease <threadId> | release <threadId> | status"

python3 - "$POOL_FILE" "$POOL_LOCK" "$cmd" "$tid" "$(worker_bots | tr '\n' ',')" <<'PY'
import json, sys, os, fcntl, datetime
pool_file, lock_file, cmd, tid, bots = sys.argv[1:6]
bots = [b for b in bots.split(",") if b]
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
os.makedirs(os.path.dirname(pool_file), exist_ok=True)
with open(lock_file, "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    try:
        pool = json.load(open(pool_file))
    except Exception:
        pool = {}
    for b in bots:
        pool.setdefault(b, {"threadId": None, "since": None})
    # Preserve retired active leases until explicitly released; never reallocate them.
    rc = 0
    if cmd == "lease":
        held = [b for b in bots if pool[b]["threadId"] == tid]
        free = [b for b in bots if pool[b]["threadId"] is None]
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
    elif cmd == "status":
        used = sum(1 for b in bots if pool[b]["threadId"])
        print(f"{used}/{len(bots)} 사용 중")
        for b in bots:
            e = pool[b]
            print(f"  {b}\t{e['threadId'] or '-'}\t{e['since'] or ''}")
    else:
        print("unknown command", file=sys.stderr); rc = 2
    tmp = pool_file + ".tmp"
    open(tmp, "w").write(json.dumps(pool, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, pool_file)
sys.exit(rc)
PY
