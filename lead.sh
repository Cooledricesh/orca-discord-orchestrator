#!/bin/zsh
# 관리 세션 런처 (v2). Orca 의 ~/orchestrator 터미널에서: ./lead.sh
# 이 세션은 Discord 에 붙지 않는다. Discord 상시 세션은 bin/lead-up.sh <상담역|접수원> 로 따로 띄운다.
# 주의: 이 세션 안에서 자기 프로세스/터미널을 kill 하거나 `orca terminal close` 를 핸들 없이 실행하지 말 것.
cd "$(dirname "$0")"
if ! orca status --json >/dev/null 2>&1; then
  echo "[lead] Orca 런타임 응답 없음 — Orca 앱 실행/CLI 등록 확인"; exit 1
fi
exec caffeinate -dims claude --dangerously-skip-permissions
