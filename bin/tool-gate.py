#!/usr/bin/env python3
"""마크 Bash 툴 게이트 (PreToolUse, matcher Bash). Jev 분류기로 allow/ask/deny 를 판정한다.

- 마크 세션(ORCA_THREAD_ID)의 Bash 만 대상. 상시 세션·그록·자비스는 건너뛴다.
- 1차: 명백히 안전한 명령(읽기 전용 셸·git 조회·add/commit·지정 운영 스크립트·작업 폴더/스크래치 쓰기·
  runs 로그 덧붙이기)은 Jev 없이 allow.
- 2차: 나머지는 OpenRouter Decisions API(Jev)에 State/Choice 로 묻는다. 최종 판정은 코드가 한다
  (allow 는 확신도 < allowThreshold, deny 는 < threshold 면 ask 로 내린다).
- 키 없음·타임아웃·네트워크·응답 형식 오류는 fail-open(allow) 하고 기록에 error 를 남긴다.
- 모드: routes.json toolGate.mode — shadow(분리 자식이 판정·기록만, 훅은 즉시 반환) /
  enforce(기본. 동기 판정, deny 는 차단, ask 는 차단 + 스레드에 ✅ 확인 요청. 소유자가 ✅ 를 누르면 60분간 같은 명령 allow) / off.
기록: $STATE_DIR_ROOT/tool-gate/<threadId>.jsonl (qv=질문 버전). 승인: tool-gate/<threadId>.approvals.json.
누적 집계: tool-gate/summary.json — 섀도에서 Jev 판정 수·스레드 수가
toolGate.reviewAt(기본 50건·10개)에 처음 닿으면 운영 로그 채널에 소유자 멘션으로 검토 알림을 한 번 올린다. 키: config.openrouter_key_file (routes.json openrouterKeyFile > $BOTS_DIR/openrouter.env, OPENROUTER_API_KEY=).
"""
import fcntl, hashlib, json, math, os, re, shlex, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

API_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_TIMEOUT_S = 3.5   # 훅 timeout 5초 안: 파이썬 기동·기록 여유 포함
GATE_BUDGET_S = 4.5   # 훅 한 번의 전체 예산 (Discord 호출은 남은 만큼만)
DISCORD_TIMEOUT_S = 1.5
APPROVAL_TTL_S = 3600
APPROVE_EMOJI = "✅"
CHOICES = ("allow", "ask", "deny")
QUESTION_VERSION = "bash-gate-v4-20261007"

SAFE_CMDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "rg", "wc", "pwd", "echo", "printf", "which", "file",
    "stat", "du", "df", "tree", "sort", "uniq", "cut", "tr", "diff", "cmp", "jq", "date", "basename",
    "dirname", "realpath", "readlink", "true", "test", "[", "cd", "sed", "find", "ps", "whoami", "uname",
    "sleep", "pgrep", "lsof",
}
SAFE_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "describe",
            "merge-base", "cat-file", "shortlog", "ls-remote", "rev-list", "check-ignore", "for-each-ref"}
SAFE_GIT_LIST = {"stash": {"list", "show"}, "worktree": {"list"}, "remote": {"-v", "show", "get-url"}}
BRANCH_READ_ARGS = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--merged", "--no-merged", "--contains"}
FIND_BAD_ARGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
OPS_SCRIPTS = {"post-result.sh", "finish-worker.sh"}
# 상태 조회만 하는 하위 명령 (명령 → 허용 첫 인자)
READ_SUBCMDS = {"launchctl": {"list", "print"}, "crontab": {"-l"}, "lms": {"ps", "ls", "status"}}
GH_READ = {("auth", "status"), ("pr", "view"), ("pr", "list"), ("pr", "status"), ("pr", "diff"), ("pr", "checks"),
           ("repo", "view"), ("run", "list"), ("run", "view")}
PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?")
PUNCT = "();<>|&\n"
# 스캐너가 쓰는 표지 문자 (원 명령에 있으면 거른다): 확장 안 되는 $·~, 리다이렉트 >, >>, <
LIT_DOLLAR, LIT_TILDE, R_OUT, R_APPEND, R_IN = "\x01", "\x02", "\x03", "\x04", "\x05"
MARKS = LIT_DOLLAR + LIT_TILDE + R_OUT + R_APPEND + R_IN
NAME = r"[A-Za-z_][A-Za-z0-9_]*"
ASSIGN = re.compile(rf"({NAME})=(.*)", re.S)
VAR_REF = re.compile(rf"\$\{{({NAME})\}}|\$({NAME})")
HEREDOC = re.compile(rf"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)({NAME}))")
DUP_FD = re.compile(r">&(?:\d+|-)(?=$|[\s;&|)])")
# 다음 명령의 동작을 바꾸는 변수는 대입만으로도 Jev 로 보낸다 (PATH=… ls, GIT_PAGER=… git log 등)
RISKY_VARS = re.compile(r"PATH|IFS|ENV|BASH_ENV|CDPATH|HOME|SHELLOPTS|BASHOPTS|PS4|PROMPT_COMMAND|"
                        r"LD_\w*|DYLD_\w*|GIT_\w*|\w*PAGER|EDITOR|VISUAL|LESS\w*|PYTHON\w*|NODE_OPTIONS|PERL5\w*|RUBYOPT")
DEV_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}
# 보고 절차에 흔한 날짜 치환만 허용한다 ($(date +%F) 등). 형식 인자 외의 옵션(-s 등)은 안 된다
DATE_SUBST = re.compile(r"\$\(date(?: \+[%A-Za-z0-9:_.-]+)?\)")
DATE_WORD = "DATE"
LOOP_WORDS = {"while", "until", "do"}  # 뒤따르는 명령을 그대로 검사한다 (done 은 단독일 때만)


def write_roots(worktree: str) -> list[str]:
    """[작업 폴더, 세션 스크래치(/private/tmp, /tmp)]. 작업 폴더가 없거나 / · 홈이면 빈 목록."""
    wt = os.path.normpath(worktree) if worktree and os.path.isabs(worktree) else ""
    if not wt or wt in ("/", os.path.expanduser("~")):
        return []
    slug, uid = re.sub(r"[^A-Za-z0-9]", "-", wt), os.getuid()
    return [wt, f"/private/tmp/claude-{uid}/{slug}", f"/tmp/claude-{uid}/{slug}"]


def has_subst(text: str) -> bool:
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`" or text.startswith("$(", i) and not DATE_SUBST.match(text, i):
            return True
        i += 1
    return False


def heredoc_end(cmd: str, i: int, delim: str, tabs: bool, quoted: bool) -> int:
    """본문을 건너뛴 위치(닫는 줄 끝). 닫는 줄이 없거나 따옴표 없는 본문에 치환이 있으면 -1."""
    while True:
        j = cmd.find("\n", i)
        end = len(cmd) if j < 0 else j
        line = cmd[i:end]
        if (line.lstrip("\t") if tabs else line) == delim:
            return end
        if j < 0 or not quoted and has_subst(line):
            return -1
        i = j + 1


def scan(cmd: str) -> str | None:
    """따옴표를 따라가며 명령 치환·프로세스 치환을 거르고, heredoc 본문·주석을 걷어내고,
    리다이렉트를 표지 문자로 바꾼다. 작은따옴표 안·이스케이프된 $ 와 따옴표 안 ~ 는 확장 안 되게 표지로. 안전하지 않으면 None."""
    out: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    state, i, n = None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if state == "'":
            state = None if c == "'" else state
            out.append({"$": LIT_DOLLAR, "~": LIT_TILDE}.get(c, c))
            i += 1
            continue
        if c == "\\":
            nxt = cmd[i + 1:i + 2]
            if nxt == "$" or nxt == "~" and state is None:
                out.append(LIT_DOLLAR if nxt == "$" else LIT_TILDE)
            elif not (nxt == "\n" and state is None):  # 따옴표 밖 \⏎ 는 줄 이음
                out.append(c + nxt)
            i += 2
            continue
        m = DATE_SUBST.match(cmd, i) if c == "$" else None
        if m:
            out.append(DATE_WORD)
            i = m.end()
            continue
        if c == "`" or cmd.startswith("$(", i):
            return None
        if state == '"':
            state = None if c == '"' else state
            out.append(LIT_TILDE if c == "~" else c)
            i += 1
            continue
        if c in "'\"":
            state = c
        elif c == "#" and (not out or out[-1][-1] in " \t\n;&|()"):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        elif c == "\n":
            out.append(c)
            i += 1
            for delim, tabs, quoted in pending:
                i = heredoc_end(cmd, i, delim, tabs, quoted)
                if i < 0:
                    return None
            pending = []
            continue
        elif c == "<":
            if cmd.startswith("<<<", i):
                out.append(f" {R_IN} ")
                i += 3
            elif cmd.startswith("<<", i):
                m = HEREDOC.match(cmd, i)
                if not m:
                    return None
                delim = next(g for g in (m.group(2), m.group(3), m.group(5)) if g is not None)
                pending.append((delim, m.group(1) == "-", m.group(5) is None or bool(m.group(4))))
                out.append(" ")
                i = m.end()
            elif cmd.startswith(("<(", "<>", "<&"), i):
                return None
            else:
                out.append(f" {R_IN} ")
                i += 1
            continue
        elif c == ">" or cmd.startswith("&>", i):
            if c == "&":
                i += 1
            else:  # 2> 의 fd 번호는 명령 인자가 아니다
                j = len(out)
                while j and out[j - 1].isdigit():
                    j -= 1
                if j < len(out) and (not j or out[j - 1][-1] in " \t\n;&|("):
                    del out[j:]
            if cmd.startswith(">(", i):
                return None
            m = DUP_FD.match(cmd, i) if c == ">" else None
            if m:  # 2>&1 · >&- : 파일을 건드리지 않는다
                i = m.end()
                continue
            if cmd.startswith(">>", i):
                out.append(f" {R_APPEND} ")
                i += 2
            else:
                out.append(f" {R_OUT} ")
                i += 2 if cmd.startswith((">|", ">&"), i) else 1
            continue
        out.append(c)
        i += 1
    return None if state or pending else "".join(out)


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class RuleCtx:
    """한 명령 안에서 앞서 한 대입·cd 를 따라간다. 모르는 값은 None (경로 검사 실패 → Jev)."""

    def __init__(self, orch_root: str, roots: list[str] | None, cwd: str | None):
        self.home = os.path.expanduser("~")
        self.orch_root = os.path.normpath(orch_root) if orch_root else ""
        self.roots = [os.path.normpath(r) for r in roots or []]
        self.vars: dict[str, str | None] = {"HOME": self.home}
        if self.orch_root:
            self.vars["ORCH_ROOT"] = self.orch_root
        self.cwds = {os.path.normpath(cwd)} if cwd and os.path.isabs(cwd) else None  # 가능한 cwd 들 (cd 실패 가능성 포함)
        self.cd_from: set[str] | None = None  # 직전 cd 전의 cwd 들
        self.aborted: set[str] = set()       # cd 가 실패해 && 사슬이 끊겼을 때의 cwd 들

    def expand(self, word: str, assign: bool = False) -> str | None:
        if word.startswith("~"):
            if word != "~" and not word.startswith("~/"):
                return None  # ~user, ~+ …
            word = self.home + word[1:]
        if assign and "~" in word:
            return None  # 대입값의 :~ 확장
        word = VAR_REF.sub(lambda m: self.vars.get(m.group(1) or m.group(2)) or m.group(0), word)
        if "$" in word:
            return None
        return word.replace(LIT_DOLLAR, "$").replace(LIT_TILDE, "~")

    def resolve(self, word: str) -> list[str] | None:
        w = self.expand(word)
        if not w or any(ch in w for ch in "*?[]{}"):
            return None  # glob·brace 확장은 대상이 불확실
        if os.path.isabs(w):
            return [os.path.normpath(w)]
        return [os.path.normpath(os.path.join(c, w)) for c in self.cwds] if self.cwds else None

    def inside(self, word: str, roots: list[str]) -> bool:
        paths = self.resolve(word)
        return bool(paths and roots) and all(any(within(p, r) for r in roots) for p in paths)

    def can_write(self, word: str, append: bool) -> bool:
        paths = self.resolve(word)
        logs = os.path.join(self.orch_root, "runs") if self.orch_root else ""
        return bool(paths) and all(p in DEV_SINKS or any(within(p, r) for r in self.roots)
                                   or append and logs and within(p, logs) and p.endswith(".md") for p in paths)

    def cd(self, args: list[str]) -> None:
        target = args[0] if args else "~"
        new = None if target == "-" or len(args) > 1 else self.resolve(target)
        self.cd_from = self.cwds if self.cwds is not None and new else None
        self.cwds = set(new) if self.cd_from is not None else None

    def chain(self, op: str) -> None:
        """cd 뒤 && 사슬 안에서는 새 cwd 만, 사슬이 끝나면(; ⏎ || | &) cd 실패 때의 cwd 도 다시 가능하다."""
        if self.cd_from is not None:
            if op == "&&":
                self.aborted |= self.cd_from
            elif self.cwds is not None:
                self.cwds |= self.cd_from
            self.cd_from = None
        if op != "&&" and self.aborted:
            if self.cwds is not None:
                self.cwds |= self.aborted
            self.aborted = set()


def safe_by_rule(cmd: str, orch_root: str = "", roots: list[str] | None = None, cwd: str | None = None) -> bool:
    """True 면 Jev 없이 allow. 조금이라도 애매하면 False (Jev 로 보낸다).
    roots = 쓰기 허용 루트 (write_roots: 작업 폴더·스크래치), cwd 기본값은 roots[0]."""
    if not cmd.strip() or any(ch in cmd for ch in MARKS):
        return False
    text = scan(cmd)
    if text is None:
        return False
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""  # 따옴표 밖 줄바꿈은 명령 구분자
        tokens = list(lex)
    except ValueError:
        return False
    ctx = RuleCtx(orch_root, roots, cwd or (roots[0] if roots else None))
    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok and not tok.strip(PUNCT):  # 연산자 토큰
            if set(tok) & set("<>()"):
                return False  # 서브셸·남은 리다이렉트 (따옴표 안 ">" 도 보수적으로)
            if segment and not safe_segment(segment, ctx):
                return False
            ctx.chain(tok)
            segment = []
        else:
            segment.append(tok)
    return True


def safe_segment(words: list[str], ctx: RuleCtx) -> bool:
    plain: list[str] = []
    it = iter(words)
    for w in it:
        if w in (R_OUT, R_APPEND, R_IN):
            target = next(it, None)
            if target is None or target in (R_OUT, R_APPEND, R_IN) or w != R_IN and not ctx.can_write(target, w == R_APPEND):
                return False  # 쓰기는 작업 폴더·스크래치·runs/*.md 덧붙이기만
        else:
            plain.append(w)
    if plain == ["done"]:
        return True
    if plain and plain[0] in LOOP_WORDS:
        if plain[0] != "do" and len(plain) == 1:
            return False
        plain = plain[1:]
        if not plain:
            return True
    assigns = []
    while plain and ASSIGN.fullmatch(plain[0]):
        assigns.append(ASSIGN.fullmatch(plain.pop(0)).groups())
    if any(RISKY_VARS.fullmatch(name) for name, _ in assigns):
        return False
    if not plain:  # 대입만: 뒤 명령의 경로 확장에 쓴다 (접두 대입 FOO=bar cmd 는 그 명령에만 적용되므로 기록 안 함)
        for name, value in assigns:
            ctx.vars[name] = ctx.expand(value, assign=True)
        return True
    head, args = plain[0], plain[1:]
    exp = ctx.expand(head)
    ops = ctx.resolve(head) if "/" in head and ctx.orch_root else None  # 절대 경로·cd 뒤 상대 경로
    if ops and all(os.path.basename(p) in OPS_SCRIPTS and os.path.dirname(p) == os.path.join(ctx.orch_root, "bin")
                   for p in ops):
        return True
    if "ORCH_ROOT" not in ctx.vars and head in {f"{p}/bin/{s}" for p in ("$ORCH_ROOT", "${ORCH_ROOT}") for s in OPS_SCRIPTS}:
        return True
    if head in ("python", "python3") or exp and PYTHON.fullmatch(os.path.basename(exp)) \
            and os.path.basename(os.path.dirname(os.path.dirname(exp))) == ".venv":  # 프로젝트 가상환경 python
        return bool(args) and (args[0] == "-c" or args[:2] in (["-m", "json.tool"], ["-m", "unittest"], ["-m", "pytest"]))
    tool = os.path.basename(exp) if exp else ""
    if tool in READ_SUBCMDS and (head == tool or tool == "lms" and exp == os.path.join(ctx.home, ".lmstudio/bin/lms")):
        return bool(args) and args[0] in READ_SUBCMDS[tool]
    if head == "gh":
        return tuple(args[:2]) in GH_READ
    if tool == "Godot" and "--headless" in args:  # 작업 폴더 프로젝트의 헤드리스 실행·테스트
        i = args.index("--path") if "--path" in args else -1
        return 0 <= i < len(args) - 1 and ctx.inside(args[i + 1], ctx.roots[:1])
    if head == "cd":
        ctx.cd([a for a in args if a not in ("-P", "-L")])
        return True
    if head == "mkdir":
        paths = [a for a in args if not a.startswith("-")]
        return bool(paths) and all(a in ("-p", "-v", "-pv", "-vp") for a in args if a.startswith("-")) \
            and all(ctx.inside(p, ctx.roots) for p in paths)
    if head == "git":
        gdir = []
        while args and args[0] in ("-C", "--no-pager"):
            if args[0] == "-C":
                gdir.append(args[1] if len(args) > 1 else "")
            args = args[2:] if args[0] == "-C" else args[1:]
        if not args:
            return False
        sub, rest = args[0], args[1:]
        if sub in ("add", "commit"):  # 로컬·되돌릴 수 있음. 작업 폴더 안에서만
            return len(gdir) <= 1 and ctx.inside(gdir[0] if gdir else ".", ctx.roots[:1])
        if sub == "branch":
            return all(a in BRANCH_READ_ARGS for a in rest)
        if sub in SAFE_GIT_LIST:
            return sub == "remote" and not rest or bool(rest) and rest[0] in SAFE_GIT_LIST[sub]
        return sub in SAFE_GIT
    if head == "sed":
        return not any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args)
    if head == "find":
        return not (set(args) & FIND_BAD_ARGS)
    return head in SAFE_CMDS


def build_request(cmd: str, cwd: str, model: str, worktree: str = "", orch_root: str = "") -> dict:
    root = orch_root or "$ORCH_ROOT"
    roots = write_roots(worktree)
    state = {"state_version": QUESTION_VERSION, "shell_command": cmd[:4000],
             "working_directory": cwd, "worktree": worktree or cwd,
             "scratch_dir": roots[1] + "/" if roots else "",
             "actor": "autonomous coding agent working in a disposable git worktree",
             "sanctioned": f"Appending to {root}/runs/<project>/<date>.md logs and calling {root}/bin/post-result.sh "
                           "or finish-worker.sh are the agent's required reporting steps.",
             "protected": "~/.claude, routes.json, bot tokens, other worktrees, the original project folder, launchd jobs, running bot processes"}
    question = {"type": "choice", "instructions": {
        "question": "Should `shell_command` run without asking the owner first?",
        "scope": "Judge only the command's direct effect. Do not judge code quality or whether the task is useful.",
    }, "criteria": {
        "allow": {"what": "Reads, builds, tests, or edits files inside the worktree or the session's scratch_dir; "
                          "local git add/commit; the sanctioned reporting steps; reversible actions.",
                  "not_for": "Anything touching protected resources, remote state, or deleting data outside the worktree."},
        "ask": {"what": "Plausibly legitimate but hard to reverse or outward-facing: git push, force/reset/clean, killing processes, "
                        "launchctl, ssh or commands on remote hosts, docker, package installs, writes outside the worktree and scratch_dir.",
                "not_for": "Plain reads, the sanctioned reporting steps, or clearly destructive/exfiltrating commands."},
        "deny": {"what": "Destructive or dangerous: rm -rf on broad or home paths, piping downloads into a shell, printing or sending secrets, editing ~/.claude or bot tokens, killing all agent sessions.",
                 "not_for": "Ordinary development commands."},
    }}
    return {"model": model, "state": state, "questions": {"gate": question}}


def parse_response(data: dict) -> tuple[str, float]:
    """Jev 응답 → (choice, confidence). 형식이 어긋나면 ValueError."""
    try:
        answer = data["answers"]["gate"]
        choice, conf = answer["choice"], answer["confidence"]
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid_response") from exc
    if choice not in CHOICES or isinstance(conf, bool) or not isinstance(conf, (int, float)) \
            or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise ValueError("invalid_response")
    return choice, float(conf)


def decide(choice: str, conf: float, threshold: float, allow_threshold: float | None = None) -> str:
    """권한은 코드가 소유한다: 확신이 낮으면 ask. allow 는 allow_threshold(없으면 threshold), deny 는 threshold 기준."""
    need = allow_threshold if choice == "allow" and allow_threshold is not None else threshold
    return choice if choice == "ask" or conf >= need else "ask"


def load_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                name, _, value = line.strip().partition("=")
                if name.strip() == "OPENROUTER_API_KEY" and value.strip().strip("\"'"):
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def call_jev(body: dict, key: str, timeout: float = JEV_TIMEOUT_S, opener=None) -> dict:
    """전체 소요(DNS 포함)를 timeout 으로 자른다. 넘으면 TimeoutError."""
    box: dict = {}

    def run():
        req = urllib.request.Request(API_URL, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
                box["data"] = json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 — 전부 fail-open
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or isinstance(box.get("error"), TimeoutError):
        raise TimeoutError("timeout")
    if "error" in box:
        raise ConnectionError("api_error")
    return box.get("data") or {}


def evaluate(cmd: str, cwd: str, cfg: dict, key_file: str, orch_root: str = "", jev=None,
             worktree: str = "", approval=None) -> dict:
    """판정 한 건. 항상 dict 를 돌려주고 예외를 올리지 않는다.
    approval: 규칙 다음·Jev 전에 부르는 승인 조회 (enforce). "approved" 면 allow, "pending" 이면 Jev 없이 ask."""
    started = time.monotonic()
    rec = {"verdict": None, "confidence": None, "source": "rule", "error": None}
    try:
        state = None
        if safe_by_rule(cmd, orch_root, write_roots(worktree), cwd if os.path.isabs(cwd or "") else None):
            rec["decision"] = "allow"
        elif approval:
            state = approval()
    except Exception:  # noqa: BLE001 — 규칙·승인 조회 오류는 Jev 로
        state = None
    if "decision" in rec:
        pass
    elif state in ("approved", "pending"):
        rec.update(decision="allow" if state == "approved" else "ask", source="approval", approval=state)
    else:
        key = load_key(key_file)
        if not key:
            rec.update(decision="allow", source="fail-open", error="missing_key")
        else:
            try:
                choice, conf = parse_response((jev or call_jev)(build_request(cmd, cwd, cfg["model"], worktree, orch_root), key))
                rec.update(verdict=choice, confidence=round(conf, 4), source="jev",
                           decision=decide(choice, conf, cfg["threshold"], cfg.get("allowThreshold")))
            except TimeoutError:
                rec.update(decision="allow", source="fail-open", error="timeout")
            except ValueError:
                rec.update(decision="allow", source="fail-open", error="invalid_response")
            except Exception:  # noqa: BLE001
                rec.update(decision="allow", source="fail-open", error="api_error")
    rec["ms"] = int((time.monotonic() - started) * 1000)
    return rec


def gate_config() -> dict:
    from config import TOOL_GATE_DEFAULTS, load_routes, openrouter_key_file, tool_gate
    try:
        data = load_routes()
        return {**tool_gate(data), "keyFile": openrouter_key_file(data), "ownerUserId": str(data.get("ownerUserId") or "")}
    except Exception:
        return dict(TOOL_GATE_DEFAULTS)  # 설정 없음·형식 오류여도 기본(shadow)으로 기록은 남긴다


def write_log(state_root: str, thread: str, rec: dict) -> None:
    d = os.path.join(state_root, "tool-gate")
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, f"{thread}.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")


def tally(state_root: str, thread: str, rec: dict, review_at: dict) -> dict | None:
    """summary.json 누적. 검토 기준에 처음 닿은 호출에만 집계를 돌려준다 (그 뒤로는 None)."""
    d = os.path.join(state_root, "tool-gate")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "summary.json")
    with open(os.path.join(d, "summary.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path) as stream:
                s = json.load(stream)
        except (OSError, ValueError):
            s = {"since": rec["ts"], "total": 0, "rule": 0, "jev": 0, "decisions": {}, "lowConfidence": 0,
                 "errors": {}, "threads": [], "notified": None}
        s["total"] += 1
        if rec["source"] == "jev":
            s["jev"] += 1
            s["decisions"][rec["decision"]] = s["decisions"].get(rec["decision"], 0) + 1
            s["lowConfidence"] += rec["verdict"] != rec["decision"]
            if thread not in s["threads"]:
                s["threads"].append(thread)
        elif rec["error"]:
            s["errors"][rec["error"]] = s["errors"].get(rec["error"], 0) + 1
        else:
            s["rule"] += 1
        due = not s["notified"] and s["jev"] >= review_at["jev"] and len(s["threads"]) >= review_at["threads"]
        if due:
            s["notified"] = rec["ts"]
        tmp = path + ".tmp"
        with open(tmp, "w") as out:
            json.dump(s, out, ensure_ascii=False)
        os.replace(tmp, path)
    return s if due else None


def discord_api():
    """(token, api) — 마크 봇 토큰과 progress-hook 의 REST 헬퍼. 없으면 (None, None)."""
    sd = os.environ.get("DISCORD_STATE_DIR")
    if not sd:
        return None, None
    import importlib.util
    spec = importlib.util.spec_from_file_location("progress_hook", os.path.join(os.path.dirname(os.path.realpath(__file__)), "progress-hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    token = mod.read_token(os.path.join(sd, ".env"))
    return (token, mod.api) if token else (None, None)


def review_message(s: dict, owner: str) -> str:
    dec = s["decisions"]
    errors = ", ".join(f"{k} {v}" for k, v in s["errors"].items()) or "없음"
    return (f"<@{owner}> 🛡 툴 게이트 섀도 기록이 검토 기준에 닿았습니다 ({s['since'][:10]}~)\n"
            f"- 마크 작업 {len(s['threads'])}개 · Bash {s['total']}건 (규칙 allow {s['rule']} · Jev {s['jev']})\n"
            f"- Jev 최종: allow {dec.get('allow', 0)} · ask {dec.get('ask', 0)} · deny {dec.get('deny', 0)} "
            f"(확신도 미달로 ask 로 내린 것 {s['lowConfidence']})\n"
            f"- fail-open: {errors}\n"
            f"오판 검토 후 enforce 전환 여부를 정하세요. 오케스트레이터 스레드에 \"게이트 기록 정리\" 요청.")


def notify_review(s: dict) -> None:
    from config import load_routes
    routes = load_routes()
    owner = routes.get("ownerUserId")
    token, api = discord_api()
    if not (token and owner):
        return
    body = {"content": review_message(s, owner), "allowed_mentions": {"users": [owner]}}
    # summary.json reviewChannel(검토를 맡은 스레드 등)이 있으면 거기로, 실패하면 운영 로그 채널로
    for channel in dict.fromkeys(c for c in (s.get("reviewChannel"), routes.get("opsLogChannelId")) if c):
        if api(token, "POST", f"/channels/{channel}/messages", body):
            return


def notify(thread: str, rec: dict) -> None:
    token, api = discord_api()
    if not token:
        return
    conf = "" if rec["confidence"] is None else f" {rec['confidence']:.2f}"
    cmd = rec["command"][:80].replace("`", "'")
    api(token, "POST", f"/channels/{thread}/messages",
                     {"content": f"🛡 게이트(섀도) {rec['decision']}{conf} · `{cmd}`", "allowed_mentions": {"parse": []}})


def approvals_path(state_root: str, thread: str) -> str:
    return os.path.join(state_root, "tool-gate", f"{thread}.approvals.json")


def load_approvals(path: str) -> dict:
    try:
        with open(path) as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_approvals(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        json.dump(data, out, ensure_ascii=False)
    os.replace(tmp, path)


def command_key(cmd: str) -> str:
    return hashlib.sha256(cmd.encode()).hexdigest()


def check_approval(path: str, thread: str, cmd: str, owner: str, rest, now: float | None = None) -> str | None:
    """"approved"(60분 안 승인·방금 소유자 ✅) / "pending"(요청 올림, 승인 없음) / None(기록 없음·만료).
    반응 조회(GET)는 요청 기록이 있을 때만."""
    now = time.time() if now is None else now
    data = load_approvals(path)
    entry = data.get(command_key(cmd))
    if not isinstance(entry, dict) or not entry.get("messageId"):
        return None
    if entry.get("approved"):
        return "approved" if now - entry["approved"] < APPROVAL_TTL_S else None
    users = rest("GET", f"/channels/{thread}/messages/{entry['messageId']}/reactions/{urllib.parse.quote(APPROVE_EMOJI)}") if owner else None
    if isinstance(users, list) and any(isinstance(u, dict) and str(u.get("id")) == owner for u in users):
        entry["approved"] = int(now)
        save_approvals(path, data)
        return "approved"
    return "pending"


def request_approval(path: str, thread: str, cmd: str, owner: str, rest, now: float | None = None) -> bool:
    """스레드에 ✅ 확인 요청을 한 번 올리고 기록한다. 이미 대기 중이면 다시 올리지 않는다. 올렸거나 대기 중이면 True."""
    now = time.time() if now is None else now
    data = load_approvals(path)
    key = command_key(cmd)
    entry = data.get(key)
    if isinstance(entry, dict) and entry.get("messageId") and not entry.get("approved"):
        return True
    if not owner:
        return False
    short = re.sub(r"\s*\n\s*", " ", cmd[:150]).replace("`", "'")
    msg = rest("POST", f"/channels/{thread}/messages", {
        "content": f"🛡 확인 필요 (<@{owner}>): `{short}`\n허용하려면 아래 {APPROVE_EMOJI} 를 누르고 스레드에 \"진행\" 이라고 쓰세요.",
        "allowed_mentions": {"users": [owner]},
        # 플러그인(discord-orca tool-gate-buttons.ts)이 소유자 클릭을 approvals.json 에 기록한다. ✅ 반응도 그대로 유효.
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "승인 (60분)", "emoji": {"name": APPROVE_EMOJI}, "custom_id": f"tgate:allow:{key}"},
            {"type": 2, "style": 4, "label": "거부", "custom_id": f"tgate:deny:{key}"}]}]})
    if not isinstance(msg, dict) or not msg.get("id"):
        return False
    # 소유자가 탭만 하도록 봇이 ✅ 를 먼저 단다. 실패해도 게시는 유효하다 (기록만 남긴다).
    try:
        reacted = rest("PUT", f"/channels/{thread}/messages/{msg['id']}/reactions/{urllib.parse.quote(APPROVE_EMOJI)}/@me") is not None
    except Exception:  # noqa: BLE001
        reacted = False
    data[key] = {"messageId": str(msg["id"]), "command": cmd[:300], "requested": int(now), "approved": None, "reacted": reacted}
    save_approvals(path, data)
    return True


def gate_rest(started: float):
    """(method, ep, body) → 응답|None. 토큰은 처음 쓸 때 읽고, timeout 은 훅 예산에서 남은 만큼."""
    box: dict = {}

    def call(method: str, ep: str, body: dict | None = None):
        if "api" not in box:
            box["token"], box["api"] = discord_api()
        if not box["token"]:
            return None
        left = GATE_BUDGET_S - (time.monotonic() - started)
        return box["api"](box["token"], method, ep, body, timeout=max(0.3, min(DISCORD_TIMEOUT_S, left)))
    return call


def enforce_output(rec: dict) -> dict | None:
    if rec["decision"] == "allow":
        return None
    reason = {"deny": "툴 게이트가 이 명령을 차단했다. 다른 방법을 찾거나 소유자에게 스레드로 알린다.",
              "ask": "툴 게이트: 되돌리기 어려운 명령이다. 실행하지 말고 스레드에 소유자 확인을 요청한 뒤 답을 기다린다."}[rec["decision"]]
    if rec["decision"] == "ask" and rec.get("approval") in ("requested", "pending"):
        reason = ("툴 게이트: 되돌리기 어려운 명령이라 소유자 확인이 필요하다. 스레드에 확인 요청을 올렸다. 실행하지 말고 기다려라. "
                  "소유자가 승인했다고 하면 같은 명령을 그대로 다시 실행한다.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def state_dir(env) -> str:
    return env.get("STATE_DIR_ROOT") or os.path.join(env.get("ORCH_ROOT") or os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "state")


def run_gate(ev: dict, thread: str, cfg: dict, env: dict, rest=None) -> dict:
    """rest: Discord REST (method, ep, body) — 테스트 주입용. enforce 에서만 승인 조회·요청을 한다."""
    started = time.monotonic()
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    bots = env.get("BOTS_DIR") or os.path.expanduser("~/.claude/channels/bots")
    worktree = env.get("CLAUDE_PROJECT_DIR") or ev.get("cwd") or ""
    enforce, owner = cfg["mode"] == "enforce", cfg.get("ownerUserId") or ""
    apath = approvals_path(state_dir(env), thread)
    rest = rest or gate_rest(started)
    rec = evaluate(cmd, ev.get("cwd") or "", cfg, cfg.get("keyFile") or os.path.join(bots, "openrouter.env"), env.get("ORCH_ROOT", ""),
                   worktree=worktree, approval=(lambda: check_approval(apath, thread, cmd, owner, rest)) if enforce else None)
    if enforce and rec["decision"] == "ask" and rec["source"] == "jev":  # deny 는 승인 대상 아님
        try:
            if request_approval(apath, thread, cmd, owner, rest):
                rec["approval"] = "requested"
        except Exception:  # noqa: BLE001 — 요청 실패면 예전처럼 차단만
            pass
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "command": cmd[:300],
           "mode": cfg["mode"], "qv": QUESTION_VERSION, **rec}
    try:
        write_log(state_dir(env), thread, rec)
    except OSError:
        pass
    return rec


def main() -> None:
    thread = os.environ.get("ORCA_THREAD_ID")
    if not thread:
        return
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    if ev.get("hook_event_name") != "PreToolUse" or ev.get("tool_name") != "Bash":
        return
    cfg = gate_config()
    if cfg["mode"] == "off":
        return
    if cfg["mode"] == "enforce":
        out = enforce_output(run_gate(ev, thread, cfg, dict(os.environ)))
        if out:
            print(json.dumps(out, ensure_ascii=False))
        return
    # shadow: 훅은 즉시 반환, 판정·기록·알림은 분리된 자식이.
    if os.fork():
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        rec = run_gate(ev, thread, cfg, dict(os.environ))
        if cfg["notify"] and rec["decision"] != "allow":
            notify(thread, rec)
        summary = tally(state_dir(os.environ), thread, rec, cfg["reviewAt"])
        if summary:
            notify_review(summary)
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 게이트 오류로 마크를 막지 않는다
