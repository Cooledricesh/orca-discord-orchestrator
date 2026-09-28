#!/usr/bin/env python3
"""접수 난이도 라우팅: Jev 분류기로 요청 난이도(simple|standard|hard)를 매겨 마크의 model/effort 를 고른다.

  classify-request.py <request-file> [--project <name>] [--engine claude|grok] [--model m] [--effort e]
  stdout 한 줄: model=<m> effort=<e> level=<l> confidence=<c> source=user|jev|default reason=<r>
  빈 model/effort 는 "엔진 기본값 사용". 항상 exit 0 (fail-open): 키 없음·API 실패·타임아웃은 source=default.

우선순위: 사용자 명시(--model/--effort) > Jev(확신도 ≥ threshold) > 기본값.
설정: routes.json "jevRouting" (없으면 DEFAULTS). API 키: OPENROUTER_API_KEY 환경변수 > jevRouting.keyFile(env 형식 파일 경로) > $BOTS_DIR/openrouter.env.
요청 본문과 프로젝트 이름만 보낸다. 키·응답 원문은 출력하지 않는다.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

LEVELS = ("simple", "standard", "hard")
MAX_REQUEST_CHARS = 4000
DEFAULTS = {
    "enabled": True,
    "apiUrl": "https://openrouter.ai/api/alpha/decisions",
    "model": "typesafe/jev-1.13-20260917",
    "threshold": 0.85,
    "timeoutSec": 3,
    "keyFile": "",
    # 빈 항목 = 엔진 기본값 (models.작업자 / 작업자Effort)
    "levels": {"simple": {"model": "sonnet", "effort": "medium"}, "standard": {}, "hard": {"model": "fable", "effort": "high"}},
}


class JevError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def settings(data):
    raw = data.get("jevRouting") if isinstance(data.get("jevRouting"), dict) else {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS and k != "levels"}}
    levels = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
    cfg["levels"] = {lv: (levels[lv] if isinstance(levels.get(lv), dict) else DEFAULTS["levels"][lv]) for lv in LEVELS}
    return cfg


def load_key(bots_dir, key_file=""):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    for path in ([Path(key_file).expanduser()] if key_file else []) + [Path(bots_dir) / "openrouter.env"]:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == "OPENROUTER_API_KEY" and value.strip().strip("\"'"):
                    return value.strip().strip("\"'")
        except (OSError, UnicodeError):
            continue
    raise JevError("missing_key")


def request_text(raw):
    # 해피가 붙이는 첫 줄 [출처 chat_id=… message_id=…] 는 분류와 무관
    lines = raw.splitlines()
    if lines and lines[0].startswith("[출처"):
        lines = lines[1:]
    return "\n".join(lines).strip()[:MAX_REQUEST_CHARS]


def difficulty_question():
    return {"type": "choice", "instructions": {
        "question": "How difficult is `request` for an AI coding/research agent working in project `project`?",
        "scope": "Judge only the effort and reasoning depth the request needs. Do not judge whether it should be done.",
    }, "criteria": {
        "simple": {"what": "Clear, small, low-risk: a lookup, status check, short answer, typo/text/config tweak, or a single-file mechanical edit.",
                   "not_for": "Anything needing design decisions, investigation across components, or debugging."},
        "standard": {"what": "Typical engineering work: a feature or bug fix touching a few files, moderate investigation, docs, or a review with clear acceptance criteria.",
                     "not_for": "Trivial one-liners or open-ended multi-system problems."},
        "hard": {"what": "Complex or ambiguous: multi-component design, hard debugging with unknown cause, concurrency/state/security-sensitive changes, large refactors, or deep research synthesis.",
                 "not_for": "Work whose steps are already spelled out and local."},
    }}


def call_jev(cfg, key, project, text, opener=None):
    body = {"model": cfg["model"], "state": {"project": project, "request": text},
            "questions": {"difficulty": difficulty_question()}}
    req = urllib.request.Request(cfg["apiUrl"], method="POST",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    timeout = float(cfg["timeoutSec"])
    use_alarm = opener is None and hasattr(signal, "setitimer")

    def expired(*_):
        raise TimeoutError

    # urllib 의 timeout 은 소켓 연산마다라서 전체 시간 상한은 타이머로 건다
    if use_alarm:
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except TimeoutError as exc:
        raise JevError("timeout") from exc
    except OSError as exc:  # URLError/HTTPError/socket.timeout 포함
        raise JevError("timeout" if "timed out" in str(exc) else "api_error") from exc
    except (UnicodeError, ValueError) as exc:
        raise JevError("invalid_response") from exc
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    try:
        answer = data["answers"]["difficulty"]
        level, confidence = answer["choice"], answer["confidence"]
        if level not in LEVELS or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise JevError("invalid_response") from exc
    return level, float(confidence)


def decide(cfg, *, explicit_model="", explicit_effort="", engine="claude", classify=None):
    """classify: () -> (level, confidence), JevError 로 실패. 사용자 명시·Grok 엔진이면 부르지 않는다."""
    if explicit_model or explicit_effort:
        return dict(model=explicit_model, effort=explicit_effort, level="", confidence="", source="user", reason="explicit")
    base = dict(model="", effort="", level="", confidence="", source="default")
    if engine != "claude":
        return dict(base, reason="engine")
    if not cfg.get("enabled", True):
        return dict(base, reason="disabled")
    try:
        level, confidence = classify()
    except JevError as exc:
        return dict(base, reason=exc.code)
    except Exception:
        return dict(base, reason="error")
    conf = f"{confidence:.2f}"
    if confidence < float(cfg["threshold"]):
        return dict(base, level=level, confidence=conf, reason="low_confidence")
    target = cfg["levels"].get(level) or {}
    return dict(model=str(target.get("model", "") or ""), effort=str(target.get("effort", "") or ""),
                level=level, confidence=conf, source="jev", reason="classified")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("request_file")
    p.add_argument("--project", default="")
    p.add_argument("--engine", default="claude")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    a = p.parse_args(argv)
    try:
        data = config.load_routes()
    except ValueError:
        data = {}
    cfg = settings(data)

    def classify():
        key = load_key(config.runtime_paths(data)["BOTS_DIR"], str(cfg["keyFile"] or ""))
        try:
            text = request_text(Path(a.request_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise JevError("no_request") from exc
        if not text:
            raise JevError("no_request")
        return call_jev(cfg, key, a.project, text)

    r = decide(cfg, explicit_model=a.model, explicit_effort=a.effort, engine=a.engine, classify=classify)
    print(" ".join(f"{k}={r[k]}" for k in ("model", "effort", "level", "confidence", "source", "reason")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
