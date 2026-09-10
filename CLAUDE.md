# 오케스트레이터

- 이 폴더에서 `./lead.sh` 로 뜬 **관리 세션**이면 `docs/ADMIN.md` 를 먼저 읽는다 (절대 규칙·구성 요소·검증 루틴). 설계는 `SPEC.md`.
- 관리 세션은 Orca 터미널의 `lead.sh` 프로세스다. **자기 자신을 죽이지 않는다**: `kill`·`pkill claude`·핸들 없는 `orca terminal close` 전에 `ps -o pid,command -p $PPID` 로 확인한다.
- 상담역·접수원·작업자(마크)로 떴다면 이 파일은 무시하고 자기 역할 지침(`sessions/<역할>/CLAUDE.md`, 시스템 프롬프트)을 따른다.
- 봇 토큰(`~/.claude/channels/bots/*.env`)을 출력·커밋하지 않는다.
