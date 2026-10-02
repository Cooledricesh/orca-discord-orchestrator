#!/usr/bin/env python3
"""작업 worktree 의 터미널 정리, 끝난 worktree 자동 삭제, 누적 보고.

  worktrees.py close-thread <threadId>   등록부가 끝난(done/failed/stopped) new-worktree 스레드의
                                         task-<threadId>-* worktree 와 그 하위 worktree 터미널을 모두 닫는다
  worktrees.py sweep                     끝난 스레드 전체에 같은 정리 (sweep.sh 안전망, 스폰 잠금 중이면 건너뜀)
  worktrees.py report [--json]           task-* worktree 목록: 상태·용량·미커밋·운영 HEAD 미병합 커밋·보존 파일·경과일·터미널 수
  worktrees.py prune [--dry-run]         자동 정리: 끝난 스레드 + 미커밋·미병합·보존 파일 없음 + 종료 후 7일 경과 worktree 를 orca worktree rm
  worktrees.py weekly                    주간 보고 메시지 미리보기
  worktrees.py periodic                  sweep.sh 용: 1시간마다 prune, 7일마다 주간 보고 (#운영-로그 메시지를 stdout 에)

보존 파일: gitignore 된 항목 중 재생성 가능(node_modules·.venv·__pycache__·빌드 산출물 등)이 아닌 것 (output/·.env.local 등). 있으면 지우지 않는다.

닫는 기준은 등록부 상태와 worktree 경로다. Orca 의 orphaned 표시는 쓰지 않는다 (CLI 로 만든 살아 있는 터미널도 orphaned).
보호: 프로젝트 원본·ORCH_ROOT·끝나지 않은 스레드의 경로는 어떤 경로로도 닫지 않는다.
"""
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from config import runtime_paths

ORCA = os.environ.get("ORCA_BIN") or os.environ.get("ORCA_CLI_COMMAND") or "orca"
PATHS = runtime_paths()
THREADS = Path(PATHS["STATE_DIR_ROOT"]) / "threads"
FINISHED = {"done", "failed", "stopped"}
TASK = re.compile(r"^task-(\d+)-[0-9a-f]{8}$")
# 스폰의 플러그인 설치(ensure_plugin)·Finder 가 만드는 변경은 미커밋 판정에서 뺀다
NOISE = {".claude/settings.json", ".claude/", ".DS_Store"}
KEEP_DAYS = int(os.environ.get("WORKTREE_KEEP_DAYS", "7"))
REGENERABLE = {"node_modules", "__pycache__", ".next", "dist", "build", ".godot", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".turbo", ".cache", "venv", "tsconfig.tsbuildinfo", "next-env.d.ts", "bun.lock", ".DS_Store", ".claude"}


def log(msg):
    print(f"[worktrees.py] {msg}", file=sys.stderr, flush=True)


def norm(p):
    return os.path.realpath(p) if p else ""


def orca_json(*args):
    try:
        r = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout).get("result") if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None


def items(result, key):
    if isinstance(result, dict):
        result = result.get(key)
    return [x for x in result if isinstance(x, dict)] if isinstance(result, list) else None


def registry():
    rows = {}
    for f in THREADS.glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("threadId"):
            rows[str(r["threadId"])] = r
    return rows


def protected(rows):
    keep = {norm(PATHS["ORCH_ROOT"])}
    try:
        routes = json.loads(Path(PATHS["ROUTES_FILE"]).read_text()).get("routes", {})
    except (OSError, ValueError, AttributeError):
        routes = {}
    keep |= {norm(v.get("path")) for v in routes.values() if isinstance(v, dict)}
    keep |= {norm(r.get("projectPath")) for r in rows.values()}
    keep |= {norm(r.get("path")) for r in rows.values() if r.get("status") not in FINISHED}
    keep.discard("")
    return keep


def thread_paths(tid, rec, worktrees):
    """그 스레드의 task-<tid>-* worktree 와 Orca 하위 worktree. 하위부터, 등록부 현재 경로는 맨 끝 (작업자 자신이 호출하면 거기서 죽는다)."""
    by_id = {w.get("id"): w for w in worktrees}
    stack = [w for w in worktrees if (m := TASK.match(Path(w.get("path") or "").name)) and m.group(1) == tid]
    found, seen = [], set()
    while stack:
        w = stack.pop()
        p = norm(w.get("path"))
        if not p or p in seen:
            continue
        seen.add(p)
        found.append(p)
        stack += [by_id[c] for c in w.get("childWorktreeIds") or [] if c in by_id]
    current = norm(rec.get("path")) if (m := TASK.match(Path(rec.get("path") or "").name)) and m.group(1) == tid else ""
    ordered = [p for p in reversed(found) if p != current]
    return ordered + ([current] if current else [])


def close_paths(paths, terminals):
    for p in paths:
        n = None if terminals is None else sum(1 for t in terminals if norm(t.get("worktreePath")) == p)
        if n == 0:
            continue
        log(f"터미널 닫음: {p} ({'?' if n is None else n}개)")
        subprocess.run([ORCA, "terminal", "close", "--worktree", f"path:{p}", "--all", "--json"], capture_output=True, timeout=60)


def eligible(rec):
    return rec.get("status") in FINISHED and rec.get("worktreeMode") == "new"


def close_thread(tid):
    rows = registry()
    rec = rows.get(tid)
    if not rec or not eligible(rec):
        return 0  # 공유 폴더·진행 중 스레드는 finish-worker 가 등록 handle 만 닫는다
    worktrees = items(orca_json("worktree", "list"), "worktrees") or []
    keep = protected(rows)
    paths = [p for p in thread_paths(tid, rec, worktrees) if p not in keep]
    close_paths(paths, items(orca_json("terminal", "list"), "terminals"))
    return 0


def sweep():
    worktrees = items(orca_json("worktree", "list"), "worktrees")
    terminals = items(orca_json("terminal", "list"), "terminals")
    if worktrees is None or terminals is None:
        log("Orca 목록 조회 실패 — 건너뜀")
        return 0
    busy = {norm(t.get("worktreePath")) for t in terminals}
    THREADS.mkdir(parents=True, exist_ok=True)
    rows = registry()
    tids = {m.group(1) for w in worktrees if (m := TASK.match(Path(w.get("path") or "").name))}
    for tid in sorted(tids):
        if not eligible(rows.get(tid) or {}):
            continue
        with (THREADS / f"{tid}.spawn.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # 스폰·재개·종료가 진행 중
            rows = registry()
            rec = rows.get(tid) or {}
            if not eligible(rec):
                continue
            keep = protected(rows)
            paths = [p for p in thread_paths(tid, rec, worktrees) if p not in keep and p in busy]
            close_paths(paths, terminals)
    return 0


def git(path, *args):
    r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def regenerable(entry):
    name = entry.rstrip("/").rsplit("/", 1)[-1]
    return name in REGENERABLE or name.startswith(".venv") or name.endswith(".pyc") or ".generated." in name


def inspect(path, size=True):
    status = git(path, "status", "--porcelain", "--ignored=matching", "--untracked-files=normal")
    lines = None if status is None else status.splitlines()
    changed = None if lines is None else [l[3:] for l in lines if not l.startswith("!!") and l[3:] not in NOISE]
    preserved = None if lines is None else [l[3:] for l in lines if l.startswith("!!") and not regenerable(l[3:])]
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main = str(Path(common).parent) if common else None
    main_head = git(main, "rev-parse", "HEAD") if main else None
    ahead = git(path, "rev-list", "--count", "HEAD", "--not", main_head) if main_head else None
    kb = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split("\t")[0] if size else ""
    return dict(sizeKB=int(kb) if kb.isdigit() else None, dirty=None if changed is None else bool(changed), changed=changed,
                preserved=preserved, unmerged=int(ahead) if ahead and ahead.isdigit() else None,
                branch=git(path, "branch", "--show-current"), mainPath=main)


def age_days(ended):
    try:
        t = datetime.datetime.strptime(ended, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 86400


def entries(rows, worktrees, terminals):
    """task-* worktree 의 메타데이터 (git·용량 검사 전). Orca 목록에서 빠진 같은 부모의 task-* 폴더도 포함."""
    known = {norm(w.get("path")) for w in worktrees if TASK.match(Path(w.get("path") or "").name)}
    paths = set(known)
    for parent in {Path(p).parent for p in known}:
        paths |= {norm(str(d)) for d in parent.iterdir() if d.is_dir() and TASK.match(d.name)}
    out = []
    for p in sorted(paths):
        tid = TASK.match(Path(p).name).group(1)
        rec = rows.get(tid) or {}
        out.append(dict(path=p, threadId=tid, project=rec.get("project"), status=rec.get("status") or "-",
                        current=norm(rec.get("path")) == p, endedAt=rec.get("endedAt"), ageDays=age_days(rec.get("endedAt")),
                        inOrca=p in known, terminals=sum(1 for t in terminals if norm(t.get("worktreePath")) == p)))
    return out


def waiting(r):
    """정리 정책의 비-git 조건: 끝난 스레드, Orca 등록, 터미널 없음. 7일 경과 여부는 따로."""
    return r["status"] in FINISHED and r["inOrca"] and r["terminals"] == 0


def clean(r):
    return r["dirty"] is False and r["unmerged"] == 0 and r["preserved"] == []


def collect(size=True):
    rows = registry()
    out = entries(rows, items(orca_json("worktree", "list"), "worktrees") or [], items(orca_json("terminal", "list"), "terminals") or [])
    for r in out:
        r.update(inspect(r["path"], size))
    return out


def label(r):
    return f"{Path(r['path']).parent.name}/{Path(r['path']).name}"


def gb(rs):
    return f"{sum(r['sizeKB'] or 0 for r in rs) / 1048576:.1f}GB"


def report(as_json):
    out = collect()
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print("상태\t용량MB\t미커밋\t미병합\t보존\t경과일\t터미널\t스레드\t경로")
    for r in sorted(out, key=lambda r: (r["status"] not in FINISHED, -(r["sizeKB"] or 0))):
        st = r["status"] + ("" if r["current"] else "(이전)")
        kept = "?" if r["preserved"] is None else (",".join(x.rstrip("/") for x in r["preserved"][:3]) or "-")
        age = "-" if r["ageDays"] is None else int(r["ageDays"])
        print(f"{st}\t{(r['sizeKB'] or 0) // 1024}\t{'예' if r['dirty'] else ('?' if r['dirty'] is None else '-')}\t"
              f"{'?' if r['unmerged'] is None else r['unmerged']}\t{kept}\t{age}\t{r['terminals']}\t{r['threadId']}\t{r['path']}{'' if r['inOrca'] else ' (Orca 미등록)'}")
    ok = [r for r in out if waiting(r) and clean(r)]
    print(f"\n전체 {len(out)}개 {gb(out)} / 자동 정리 대상(끝남·미커밋·미병합·보존 파일 없음) {len(ok)}개 {gb(ok)}, "
          f"그중 {KEEP_DAYS}일 경과 {sum(1 for r in ok if (r['ageDays'] or 0) >= KEEP_DAYS)}개")
    return 0


def prune(dry):
    """끝난 스레드 + 미커밋·미병합·보존 파일 없음 + 종료 후 KEEP_DAYS 일 경과 worktree 를 orca worktree rm 으로 지운다. 지운 것을 stdout 에."""
    worktrees, terminals = items(orca_json("worktree", "list"), "worktrees"), items(orca_json("terminal", "list"), "terminals")
    if worktrees is None or terminals is None:
        log("Orca 목록 조회 실패 — 건너뜀")
        return []
    THREADS.mkdir(parents=True, exist_ok=True)
    removed = []
    for r in entries(registry(), worktrees, terminals):
        if not waiting(r) or (r["ageDays"] or 0) < KEEP_DAYS:
            continue
        with (THREADS / f"{r['threadId']}.spawn.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # 재개 스폰 등이 진행 중
            rows = registry()
            rec = rows.get(r["threadId"]) or {}
            if rec.get("status") not in FINISHED or (age_days(rec.get("endedAt")) or 0) < KEEP_DAYS or r["path"] in protected(rows):
                continue
            r.update(inspect(r["path"]))
            if not clean(r):
                continue
            if dry:
                print(f"(dry-run) {label(r)}\t{(r['sizeKB'] or 0) // 1024}MB")
                continue
            res = subprocess.run([ORCA, "worktree", "rm", "--worktree", f"path:{r['path']}", "--force", "--json"], capture_output=True, text=True, timeout=300)
            if res.returncode == 0 and not Path(r["path"]).exists():
                log(f"worktree 정리: {r['path']}")
                removed.append(r)
            else:
                log(f"worktree 정리 실패: {r['path']} {(res.stderr or res.stdout).strip()[:200]}")
    return removed


def weekly_message():
    out = collect()
    if not out:
        return ""
    ok = [r for r in out if waiting(r) and clean(r)]
    review = sorted((r for r in out if r["status"] in FINISHED and not clean(r)), key=lambda r: -(r["sizeKB"] or 0))
    old = [r for r in out if r["status"] not in FINISHED]
    lines = [f"📋 주간 worktree 보고 — 전체 {len(out)}개 {gb(out)} · 자동 정리 대기({KEEP_DAYS}일 미경과) {len(ok)}개 {gb(ok)} · 검토 필요 {len(review)}개 {gb(review)} · 진행 중 스레드 {len(old)}개 {gb(old)}"]
    if review:
        lines.append("검토 필요 (끝난 작업, 미커밋·미병합·보존 파일 있음):")
    for r in review:
        why = [f"미병합 {r['unmerged']}" if r["unmerged"] else "", "미커밋" if r["dirty"] else "",
               ("보존 " + ",".join(x.rstrip("/") for x in r["preserved"][:3])) if r["preserved"] else "", "확인 불가" if r["dirty"] is None or r["unmerged"] is None else ""]
        line = f"- {label(r)} {(r['sizeKB'] or 0) / 1048576:.1f}GB {' · '.join(w for w in why if w)}"
        if sum(len(l) + 1 for l in lines) + len(line) > 1800:
            lines.append(f"… 외 {len(review) - (len(lines) - 2)}개")
            break
        lines.append(line)
    lines.append("전체 표: `python3 bin/worktrees.py report`")
    return "\n".join(lines)


def periodic():
    """sweep.sh 용: 1시간마다 prune, 7일마다 주간 보고. #운영-로그에 올릴 메시지를 NUL 구분으로 stdout 에."""
    stamp = Path(PATHS["STATE_DIR_ROOT"]) / "worktrees.json"
    try:
        st = json.loads(stamp.read_text())
    except (OSError, ValueError):
        st = {}
    now, msgs = time.time(), []
    if now - st.get("lastPrune", 0) >= 3600:
        st["lastPrune"] = now
        removed = prune(False)
        if removed:
            msgs.append(f"🧹 worktree 자동 정리 {len(removed)}개 ({KEEP_DAYS}일 경과·미커밋·미병합 없음): " + ", ".join(label(r) for r in removed)[:1700])
    if now - st.get("lastReport", 0) >= 7 * 86400:
        st["lastReport"] = now
        msgs.append(weekly_message())
    msgs = [m for m in msgs if m]
    stamp.write_text(json.dumps(st))
    sys.stdout.write("".join(m + "\0" for m in msgs))
    return 0


def main(argv):
    if len(argv) == 2 and argv[0] == "close-thread" and argv[1].isdigit():
        return close_thread(argv[1])
    if argv == ["sweep"]:
        return sweep()
    if argv[:1] == ["report"] and argv[1:] in ([], ["--json"]):
        return report(bool(argv[1:]))
    if argv[:1] == ["prune"] and argv[1:] in ([], ["--dry-run"]):
        removed = prune(bool(argv[1:]))
        for r in removed:
            print(f"{label(r)}\t{(r['sizeKB'] or 0) // 1024}MB")
        return 0
    if argv == ["weekly"]:
        print(weekly_message())
        return 0
    if argv == ["periodic"]:
        return periodic()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
