#!/usr/bin/env python3
"""worker-spawn-lock.py <lockfile> <script> [args...] — 락을 잡은 채로 script 를 실행한다 (스레드별 spawn/finish 직렬화)."""
import fcntl
import os
import subprocess
import sys

lock, script, *args = sys.argv[1:]
with open(lock, 'a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('같은 스레드의 spawn/finish 가 이미 진행 중', file=sys.stderr)
        raise SystemExit(1)
    env = dict(os.environ, ORCA_WORKER_SPAWN_LOCK_FD=str(handle.fileno()), ORCA_WORKER_SPAWN_LOCK_PARENT=str(os.getpid()))
    raise SystemExit(subprocess.call([script, *args], env=env, pass_fds=(handle.fileno(),)))
