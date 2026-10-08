#!/usr/bin/env python3
"""Shared configuration helpers. Reading configuration never creates runtime files."""
import ast
import json
import os
from pathlib import Path
import shlex
import sys

SOURCE_ROOT = Path(__file__).resolve().parent.parent
ROLES = ("상담역", "접수원", "작업자", "리뷰어", "비전")
ENGINES = ("claude", "grok")
TOOL_GATE_MODES = ("shadow", "enforce", "off")
TOOL_GATE_DEFAULTS = {"mode": "shadow", "model": "typesafe/jev-1.13-20260917", "threshold": 0.85,
                      "allowThreshold": 0.5, "notify": False, "reviewAt": {"jev": 50, "threads": 10}}


def absolute(value):
    return str(Path(value).expanduser().resolve())


def root_path():
    return Path(absolute(os.environ.get("ORCH_ROOT", SOURCE_ROOT)))


def routes_path():
    return Path(absolute(os.environ.get("ROUTES_FILE", root_path() / "routes.json")))


def load_routes():
    try:
        data = json.loads(routes_path().read_text())
        if not isinstance(data, dict):
            raise ValueError("expected object")
        for key in ("enabled", "bots", "models", "review", "routes"):
            if not isinstance(data.get(key, {}), dict):
                raise ValueError(f"{key}: expected object")
        if not isinstance(data.get("bots", {}).get("workers", []), list):
            raise ValueError("bots.workers: expected array")
        if any(not isinstance(r, dict) for r in data.get("routes", {}).values()):
            raise ValueError("routes: expected route objects")
        if not isinstance(data.get("toolGate", {}), dict):
            raise ValueError("toolGate: expected object")
        for key in ("stateRoot", "codexAuthFile", "openrouterKeyFile"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"{key}: expected path string")
    except FileNotFoundError:
        raise ValueError(f"설정 없음: {routes_path()} — python3 bin/setup.py init 먼저 실행") from None
    except (ValueError, OSError) as exc:
        # Do not echo JSON contents: local settings may contain sensitive values.
        raise ValueError(f"설정 JSON 형식/접근 오류: {routes_path()} ({type(exc).__name__})") from None
    # 작업자 항목 오류는 위치만 알린다 (값은 출력하지 않는다).
    for i, item in enumerate(data.get("bots", {}).get("workers", [])):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"bots.workers[{i}]: 봇 이름 문자열 또는 {{name, engine}} 객체 필요")
        if item.get("engine", "claude") not in ENGINES:
            raise ValueError(f"bots.workers[{i}].engine: {' | '.join(ENGINES)} 만 허용")
        if any(not isinstance(item.get(k, ""), str) for k in ("model", "effort")):
            raise ValueError(f"bots.workers[{i}]: model/effort 는 문자열")
    return data


def worker_entries(data):
    """bots.workers → [{name, engine, model, effort}]. 문자열 항목은 claude 엔진."""
    result = []
    for item in data.get("bots", {}).get("workers", []):
        if isinstance(item, str):
            item = {"name": item}
        result.append({"name": item.get("name", ""), "engine": item.get("engine", "claude"),
                       "model": item.get("model", ""), "effort": item.get("effort", "")})
    return result


def tool_gate(data):
    """toolGate 설정 + 기본값. 잘못된 값은 기본값으로 (훅은 설정 오류로 마크를 막지 않는다)."""
    raw = data.get("toolGate") if isinstance(data.get("toolGate"), dict) else {}
    cfg = dict(TOOL_GATE_DEFAULTS)
    if raw.get("mode") in TOOL_GATE_MODES:
        cfg["mode"] = raw["mode"]
    if isinstance(raw.get("model"), str) and raw["model"]:
        cfg["model"] = raw["model"]
    if type(raw.get("threshold")) in (int, float) and 0 < raw["threshold"] <= 1:
        cfg["threshold"] = float(raw["threshold"])
    if type(raw.get("allowThreshold")) in (int, float) and 0 < raw["allowThreshold"] <= 1:
        cfg["allowThreshold"] = float(raw["allowThreshold"])
    if isinstance(raw.get("notify"), bool):
        cfg["notify"] = raw["notify"]
    review = raw.get("reviewAt") if isinstance(raw.get("reviewAt"), dict) else {}
    cfg["reviewAt"] = {k: review[k] if type(review.get(k)) is int and review[k] > 0 else v
                       for k, v in TOOL_GATE_DEFAULTS["reviewAt"].items()}
    return cfg


def role_enabled(data, role):
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")
    enabled = data.get("enabled", {}).get(role)
    if enabled is not None:
        return enabled is True
    if role == "비전":
        return any(r.get("kind") == "lounge" for r in data.get("routes", {}).values())
    return bool(data.get("bots", {}).get("workers" if role == "작업자" else role))


def enabled_bots(data):
    result = []
    for role in ROLES:
        if not role_enabled(data, role):
            continue
        if role == "작업자":
            names = [e["name"] for e in worker_entries(data)]
        elif role == "비전":
            names = [r.get("bot", "") for r in data.get("routes", {}).values() if r.get("kind") == "lounge"]
        else:
            names = [data.get("bots", {}).get(role, "")]
        result.extend((role, name) for name in names)
    return result


def runtime_paths(data=None):
    if data is None:
        data = load_routes() if routes_path().exists() else {}
    root = root_path()
    channel_root = absolute(data.get("stateRoot", "~/.claude/channels"))
    source_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    return {
        "ORCH_ROOT": str(root),
        "ROUTES_FILE": str(routes_path()),
        "STATE_DIR_ROOT": absolute(os.environ.get("STATE_DIR_ROOT", root / "state")),
        "BOTS_DIR": absolute(os.environ.get("BOTS_DIR", Path(channel_root) / "bots")),
        "ORCH_CODEX_AUTH_FILE": absolute(os.environ.get("ORCH_CODEX_AUTH_FILE") or data.get("codexAuthFile") or Path(source_home) / "auth.json"),
    }


def openrouter_key_files(data):
    """OpenRouter 키 파일 후보 (앞이 우선): routes.json openrouterKeyFile(다른 에이전트의 .env 등) > $BOTS_DIR/openrouter.env."""
    extra = data.get("openrouterKeyFile") or ""
    return ([absolute(extra)] if extra else []) + [str(Path(runtime_paths(data)["BOTS_DIR"]) / "openrouter.env")]


def openrouter_key_file(data):
    """OPENROUTER_API_KEY 가 들어 있는 첫 후보 파일 (없으면 마지막 후보). 값은 읽기만 하고 출력하지 않는다."""
    files = openrouter_key_files(data)
    for path in files:
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == "OPENROUTER_API_KEY" and value.strip().strip("\"'"):
                    return path
        except (OSError, UnicodeError):
            continue
    return files[-1]


def query(data, expression):
    """Keep the existing '["key"]["child"]' shell API without executing Python."""
    def walk(node):
        if isinstance(node, ast.Name) and node.id == "d":
            return data
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            key = node.slice.value
            if isinstance(key, (str, int)):
                return walk(node.value)[key]
        raise ValueError("설정 경로에는 키/인덱스만 사용 가능")
    return walk(ast.parse("d" + expression, mode="eval").body)


def main():
    verb, *args = sys.argv[1:]
    if verb == "shell":
        for key, value in runtime_paths().items():
            print(f"export {key}={shlex.quote(value)}")
    elif verb == "enabled":
        return 0 if role_enabled(load_routes(), args[0]) else 1
    elif verb == "workers":
        # 셸용: name<TAB>engine<TAB>model<TAB>effort. 인자로 엔진을 주면 그 엔진만.
        for e in worker_entries(load_routes()):
            if not args or e["engine"] == args[0]:
                print("\t".join((e["name"], e["engine"], e["model"], e["effort"])))
    elif verb == "get":
        value = query(load_routes(), args[0])
        print(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else "" if value is None else value)
    else:
        raise ValueError("usage: config.py shell|get <expression>|enabled <role>|workers [engine]")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, IndexError, TypeError, SyntaxError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        sys.exit(1)
