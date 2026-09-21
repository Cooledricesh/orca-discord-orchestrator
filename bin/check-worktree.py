#!/usr/bin/env python3
"""Reject shared directories and unrelated checkouts before starting a worker."""
from pathlib import Path
import subprocess
import sys


def git(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args], stderr=subprocess.DEVNULL, text=True).strip()


def validate(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError("원본 폴더에서 작업자를 실행할 수 없습니다")
    if Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        raise ValueError("작업 경로가 worktree 루트가 아닙니다")
    source_common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    target_common = Path(git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if source_common != target_common:
        raise ValueError("원본과 다른 저장소의 worktree입니다")


if __name__ == "__main__":
    try:
        validate(*sys.argv[1:])
    except (ValueError, OSError, subprocess.CalledProcessError, TypeError) as exc:
        print(f"worktree 격리 확인 실패: {exc}", file=sys.stderr)
        sys.exit(1)
