#!/bin/zsh
# Only called after an owner-confirmed model change. Restart exactly one role.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
role="${1:-}"
case "$role" in
  상담역|접수원)
    orca_ok || die "Orca unavailable"
    p="$(cat "$STATE_DIR_ROOT/leads/$role.pid" 2>/dev/null || true)"
    h="$(cat "$STATE_DIR_ROOT/leads/$role.term" 2>/dev/null || true)"
    # Refuse an unresolvable handle before stopping the recorded Claude PID.
    if pid_is "$p" claude; then
      [[ "$h" == term_* ]] || die "missing terminal handle"
      bot="$(route_get "[\"bots\"][\"$role\"]")"
      info="$(orca terminal show --terminal "$h" --json)"
      print -r -- "$info" | python3 -c 'import json,os,sys;t=json.load(sys.stdin)["result"]["terminal"];sys.exit(0 if t.get("agentIdentity")=="claude" and os.path.realpath(t.get("worktreePath", ""))==os.path.realpath(sys.argv[1]) and sys.argv[2] in t.get("title", "") else 1)' "$ORCH_ROOT" "$bot" || die "terminal identity mismatch"
      kill -TERM "$p"
      for i in {1..20}; do pid_is "$p" claude || break; sleep 0.25; done
      pid_is "$p" claude && die "Claude did not stop"
      orca terminal close --terminal "$h" --json >/dev/null
    fi
    "$ORCH_ROOT/bin/lead-up.sh" "$role" >/dev/null
    p="$(cat "$STATE_DIR_ROOT/leads/$role.pid")"
    pid_is "$p" claude && plugin_child "$p"
    ;;
  리뷰어)
    old="$(cat "$STATE_DIR_ROOT/jarvis.pid" 2>/dev/null || true)"
    "$ORCH_ROOT/bin/jarvis-restart.sh"
    for i in {1..20}; do
      p="$(cat "$STATE_DIR_ROOT/jarvis.pid" 2>/dev/null || true)"
      if [[ "$p" != "$old" ]] && pid_is "$p" jarvis/server.ts; then exit 0; fi
      sleep 0.5
    done
    exit 1
    ;;
  *) die "unsupported role";;
esac
