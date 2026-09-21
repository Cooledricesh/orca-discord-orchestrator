#!/usr/bin/env python3
"""Serialize lead startup; keep the lock through startup retries."""
import fcntl
import os
import subprocess
import sys

lock, script, *args = sys.argv[1:]
with open(lock, "a") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("같은 역할의 기동이 이미 진행 중입니다", file=sys.stderr)
        raise SystemExit(1)
    env = dict(os.environ, ORCA_LEAD_START_LOCK_FD=str(handle.fileno()), ORCA_LEAD_START_LOCK_PARENT=str(os.getpid()))
    raise SystemExit(subprocess.call(["zsh", script, *args], env=env, pass_fds=(handle.fileno(),)))
