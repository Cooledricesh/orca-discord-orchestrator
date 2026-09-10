# 오케스트레이터 설계 (2026-09-09)

목적: AI 봇 여럿을 역할과 Discord 공간으로 나눠, 멘션 없이 각자의 공간에서 대화한다. 봇 간 자동 협업은 없다. 봇 사이의 전달은 사람이 경로·설명을 옮기는 것으로 충분하다.

## 봇 5종
| 봇 | 역할 | 두뇌 | 받는 메시지 | cwd | 수명 |
|---|---|---|---|---|---|
| 프라이데이 | 기획·상담 | Claude (플러그인 세션) | #프라이데이 최상위+스레드, DM | `sessions/상담역` | 상시. 재시작은 사용자가 DM 으로 |
| 해피 | 접수·배정 | Claude sonnet | 프로젝트 채널 최상위 + 살아있는 작업자가 없는 스레드 | `sessions/접수원` | 상시. 매일 04:00 컨텍스트 초기화 |
| 마크1~4 | 프로젝트 수행·통합 | Claude fable (풀 4개) | 자기 스레드만 | 프로젝트 폴더 (동시 작업이면 child worktree) | 스레드 하나. "종료" 또는 30분 무응답이면 정리 |
| 비전 | 별도 상담·독립 작업 | Codex app-server (`codex-worker/`) | 비전 라운지 최상위 + 하위 스레드 (소유자만) | routes 의 라운지 path | 메인 세션 하나. 사용자가 `bin/vision.sh` 로 켜고 끈다. stop 하지 않은 채 죽으면 leads-check 가 resume |
| 자비스 | 검수 | Codex `exec` 읽기 전용 (`jarvis/`) | `@자비스` 멘션 — 소유자·프라이데이·마크가 쓴 것만 | 등록부 path 또는 라우트 path | 상시 데몬 (launchd) |

봇 토큰: `~/.claude/channels/bots/<봇>.env` (`DISCORD_BOT_TOKEN`, `DISCORD_APP_ID`). git 밖. 봇 = 프로세스 1:1.
봇은 봇의 메시지를 받지 않는다 (플러그인·비전·자비스 모두). 자비스의 답도 사람이 읽고 옮긴다.

## Claude 세션의 수신 제한 (플러그인 포크 `plugin/discord-orca`, env 로 제어)
- `DISCORD_ONLY_CHATS=<id>`: 그 chat 만 (마크: 자기 스레드).
- `DISCORD_TOP_LEVEL_ONLY=1` + `DISCORD_THREAD_REGISTRY=<dir>`: 스레드는 등록부가 active 가 아닐 때만 (해피).
- `DISCORD_IGNORE_OTHER_BOT_MENTIONS=1`: 다른 봇만 멘션한 메시지는 버림 (자비스 몫).
- `DISCORD_PRESENCE`, `DISCORD_ACTIVITY_FILE` (sweep 의 무응답 판정용).
- 봇 작성 메시지는 항상 버림. `access.json` 은 `dmPolicy: allowlist` + `groups: {채널: {requireMention: false, allowFrom: [소유자]}}`. DM 은 상담역만 (`allowFrom: [소유자]`). `dmPolicy: disabled` 는 길드까지 막으니 쓰지 않는다.
- 세션은 cwd 폴더의 project 설치 + `--dangerously-load-development-channels plugin:discord-orca@orca-local` 로 저장소의 플러그인을 직접 띄운다 (캐시 사본 아님). 토큰 없는 세션(사용자가 직접 켠 claude)에서는 플러그인이 도구 없이 대기한다. 기동마다 "local development" 프롬프트가 뜨고 스크립트가 자동 수락한다.

## 흐름
1. 프로젝트 채널 최상위 메시지 → 해피: `open-thread.sh` 로 스레드, `spawn-worker.sh` 로 마크 스폰, 스레드에 `▶ 마크 N 배정` 한 줄.
2. 마크: 스레드에서 사용자와 직접 대화. 산출물은 Agent 툴로 위임. 진행 표시는 훅(`bin/progress-hook.py`)이 `⏳ N · 도구` 를 올리고 턴이 끝나면 지운다 (턴 동안 typing 유지). 상담역·접수원도 같은 훅을 쓴다 (대상 chat 은 `.activity` 파일의 chatId). 결과는 `post-result.sh` 카드 + 원 메시지 ✅/❌ + `runs/<project>/` 로그. 세션은 남아 후속 질문을 받는다.
3. "종료"/"중단" → 마크가 `finish-worker.sh` (스레드 보관, 풀 반납, 터미널 종료, 대기열 하나 스폰). 30분 무응답 → `sweep.sh` 가 같은 정리.
4. 종료된 스레드에 다시 쓰면 해피가 그 메시지로 새 마크를 띄운다. 이어가려면 사용자가 인계 파일 경로를 메시지에 적는다. 직전 Claude 세션 복원은 사용자가 "세션 이어서" 라고 명시할 때만 `--resume`.
5. 풀이 비면 `queued` 로 등록, 다음 finish 가 `retry-queued.sh` 로 스폰.
6. #프라이데이에서 "X 프로젝트에서 시작해" → 프라이데이가 직접 스레드 생성 + spawn (`roles/상담역-작업넘기기.md`).

## 인계 (최소)
사용자가 요청할 때만 마크가 `<path>/docs/handoffs/<threadId>-<시각>.md` 를 쓰고 경로 한 줄을 스레드에 올린다. 비전에게 넘길 때는 같은 한 줄을 비전 라운지에도 올린다. 등록·검증·자동 탐색은 없다. 다음 마크/비전은 사용자가 붙여넣은 경로만 읽는다.

## 비전 (`bin/vision.sh`, `codex-worker/`)
- `fresh [요청파일]` 새 세션 / `resume` 직전 세션 복원 / `attach` TUI 재부착 / `stop` / `status`. 등록부 `state/vision.json`, 런타임 `state/vision.codex/`.
- 브리지(`service.ts`)가 Discord 게이트웨이와 `codex app-server` 를 소유한다. Orca 터미널의 TUI 는 같은 app-server 의 클라이언트라 닫아도 세션이 산다.
- 대화는 Discord 로만 한다. 터미널 TUI 는 출력 확인용이며 다른 클라이언트(브리지)가 넣은 Discord 입력은 그리지 않는다. 수신 확인(👀 + typing, 전달 실패 시 ⚠️)과 진행 카드(`⏳ N · 명령/파일/서브 에이전트`, app-server `item/started` 기반, 턴 끝에 삭제, 턴 동안 typing 유지)는 브리지가 하며 모델 컨텍스트와 무관.
- 게이트: 길드 일치 ∧ 라운지 또는 그 하위 스레드 ∧ 소유자 ∧ 봇·웹훅 아님 ∧ 다른 봇만 멘션한 것 아님. 입력은 `{chat_id, message_id, text, attachments}` JSON 으로 모델에 간다. 진행 중이면 `turn/steer`, 아니면 `turn/start`.
- 도구: `discord_reply`(chat_id 필수), `discord_create_thread`, `discord_fetch_messages`, `discord_download_attachment`. `approvalPolicy: never`, `sandbox: danger-full-access` (마크의 `--dangerously-skip-permissions` 와 같은 수준). 승인·질문 UI 는 없다.
- 역할 지침 `roles/비전.md` 가 `developerInstructions` 로 들어간다. 모델·effort 는 `routes.json models.비전`, 비면 `~/.codex/config.toml`.

## 자비스 (`jarvis/`)
- 게이트(`gate.ts`): 작성자가 소유자 또는 신뢰 봇(프라이데이·마크1~4의 `DISCORD_APP_ID`) ∧ 길드 일치 ∧ 채널(스레드면 부모)이 routes 또는 #프라이데이 ∧ 자비스 멘션 ∧ 미처리. 봇은 (봇, chat) 당 60초 3회.
- 브리프 형식(봇·사람 공통): `<@자비스ID> 검수 요청 [project|기획]` + `대상 / 확인 항목 / 판단 기준 / 제외`. 누가 쓰나: #프라이데이 → 프라이데이, 살아있는 마크 스레드 → 마크, 그 외 → 사용자 직접. 둘 다 **사용자가 요청했을 때만**.
- 흐름: 👀 → 스레드 이력(최대 200개/40k자, 이후는 delta) + 등록부 path/baseRef + `git status`/`diff --stat` → `⏳ 자비스 검토 중…` 5초 edit → 최종 답. 1900자 초과면 요약 embed + 전문을 `runs/<project>/jarvis-*.md` 첨부.
- Codex 스레드 = Discord chat 1:1 (`state/jarvis/threads/<chatId>.json`, `codex exec resume`). 격리 `CODEX_HOME=state/jarvis/codex-home` (read-only, approval never). 보관은 기억을 지우지 않고 삭제·30일 gc 만 지운다. #프라이데이 스레드는 `~/.jarvis/general-cwd` 에서 git 블록 없이.
- 인증: `~/.codex/auth.json` 을 codex-home 에 없을 때만 복사. 오류 시 `codex login` → `bun jarvis/server.ts reauth` → 데몬 재시작.

## 디렉터리
```
routes.json        채널ID → {name, path, [writeDir], [kind: lounge, bot]}, guildId, ownerUserId, bots, models, emojis, opsLogChannelId
plugin/            로컬 마켓플레이스 orca-local / discord-orca (게이트 env 포크)
codex-worker/      비전 브리지 (service, control, core, discord, storage, rpc) + 테스트
jarvis/            자비스 데몬
roles/             작업자.md(마크, --append-system-prompt-file), 비전.md, 자비스.md, 상담역-온보딩/작업넘기기 (온디맨드)
sessions/<역할>/   상시 세션 cwd (CLAUDE.md = 역할 지침)
bin/               lead-up, leads-check, open-thread, spawn-worker, finish-worker, stop-worker, retry-queued, sweep, pool, post-result, progress-hook, vision, jarvis-up/down
templates/         progress-settings.json (진행 훅: 마크·상담역·접수원 공통)
state/             threads/<id>.json 등록부 (active|queued|done|failed|stopped) + .pid .activity .prompt.md, pool.json, leads/, vision.json, vision.codex/, jarvis/, log/  (git 제외)
runs/<project>/    작업 로그
launchd/           leads-check(5분), 접수원-restart(04:00), sweep(5분), jarvis(KeepAlive)
```
STATE_DIR (플러그인 토큰·access): `~/.claude/channels/roles/<역할>/`, `~/.claude/channels/workers/<스레드id>/` (종료 시 삭제), `~/.claude/channels/workers/vision/`.
