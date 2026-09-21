#!/bin/zsh
# 라우트/역할 설정을 다시 읽는다. launchd 관리 여부를 보존한다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
role_enabled 리뷰어 || { print -- '자비스: 비활성'; exit 0; }
if launchctl print "gui/$(id -u)/ai.orca.jarvis" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/ai.orca.jarvis"
else
  "$ORCH_ROOT/bin/jarvis-down.sh"
  "$ORCH_ROOT/bin/jarvis-up.sh"
fi
