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
        for key in ("stateRoot", "codexAuthFile"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"{key}: expected path string")
        return data
    except FileNotFoundError:
        raise ValueError(f"설정 없음: {routes_path()} — python3 bin/setup.py init 먼저 실행") from None
    except (ValueError, OSError) as exc:
        # Do not echo JSON contents: local settings may contain sensitive values.
        raise ValueError(f"설정 JSON 형식/접근 오류: {routes_path()} ({type(exc).__name__})") from None


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
            names = data.get("bots", {}).get("workers", [])
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
    elif verb == "get":
        value = query(load_routes(), args[0])
        print(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else "" if value is None else value)
    else:
        raise ValueError("usage: config.py shell|get <expression>|enabled <role>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, IndexError, TypeError, SyntaxError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        sys.exit(1)
