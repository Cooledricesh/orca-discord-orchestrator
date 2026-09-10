# orchestrator

Discord 를 컨트롤 플레인으로 쓰는 Claude Code · Codex 다중 에이전트 오케스트레이터.
채널과 스레드가 곧 컨텍스트 경계이고, 봇 하나가 프로세스 하나이자 컨텍스트 하나다. 세션 터미널은 [Orca](https://github.com/stablyai/orca) 가 관리한다.

## 설계

Claude Code 의 Discord 채널 플러그인은 프로세스 하나에 봇 연결 하나, 컨텍스트 하나다. 봇 하나로 채널 전부를 받으면 기획 대화와 프로젝트 작업 지시가 한 컨텍스트에 쌓이고 채널 분리는 의미를 잃는다. 이 저장소는 그 제약을 뒤집어 **컨텍스트의 수명에 따라 봇을 나눈다.**

| 수명 | 봇 | 두뇌 | 받는 메시지 | 컨텍스트 |
|---|---|---|---|---|
| 상시 | 프라이데이 (기획·상담) | Claude Code | 자기 채널 + DM | 유지. 재시작은 사용자가 지시 |
| 상시 | 해피 (접수·배정) | Claude Code, sonnet | 프로젝트 채널 최상위 메시지 | 매일 04:00 초기화 |
| 스레드 | 마크 1~4 (수행) | Claude Code, 풀 4개 | 자기 스레드만 | 스레드 = 세션 = 프로젝트 폴더. "종료" 또는 30분 무응답이면 반납 |
| 세션 | 비전 (독립 작업) | Codex app-server | 비전 라운지 | `bin/vision.sh` 로 기동·복원·종료 |
| 상시 | 자비스 (검수) | Codex exec, 읽기 전용 | `@자비스` 멘션 | Discord 스레드 = Codex 스레드 1:1 |

원칙:

- **봇은 봇의 메시지를 받지 않는다.** 모든 게이트가 봇·웹훅 작성자를 버린다. 자비스의 검수 결과를 포함해 봇 사이의 전달은 사람이 한다. LLM 홉을 쌓지 않고, 봇끼리 서로를 호출하는 루프를 구조적으로 배제한다.
- **작업 세션은 결과를 낸 뒤에도 남는다.** 후속 질문은 같은 컨텍스트로 받는다. 정리는 사용자의 "종료" 또는 sweep 의 무응답 판정으로만 일어난다.
- **검수는 다른 모델 계열이 한다.** 자비스와 비전은 Codex 다. 같은 모델이 자기 결과를 검토하게 두지 않는다.
- **인계는 파일 경로 한 줄이다.** 사용자가 요청할 때만 마크가 `docs/handoffs/` 에 문서를 쓰고 경로를 올린다. 다음 세션은 사용자가 붙여넣은 경로만 읽는다.
- **상태는 파일이다.** 스레드 등록부, 풀, 세션 pid 는 전부 `state/` 아래 JSON 이며 launchd 의 sweep 과 leads-check 가 언제 돌아도 안전하도록 설계했다.

비목표:

- 봇 간 자동 협업 프로토콜, Discord 안의 권한 승인 UI, 등록형 인계 장치. 초기 버전에 있었으나 운용 후 제거했다.
- 범용성. 한 사람의 macOS 환경에서 굴리는 구성이며 그대로 돌리려면 아래 전제 조건이 모두 필요하다.

## 흐름

1. 프로젝트 채널 최상위 메시지 → 해피가 스레드를 열고 `spawn-worker.sh` 로 마크를 띄운다. 풀이 차 있으면 `queued` 로 등록하고 다음 반납 때 스폰한다.
2. 마크는 스레드에서 사용자와 직접 대화한다. 진행 표시는 훅이 `⏳ N · 도구` 카드로 올리고 턴이 끝나면 지운다. 결과는 카드 하나와 원 메시지 ✅/❌, `runs/<project>/` 로그로 남긴다.
3. "종료" → `finish-worker.sh` 가 스레드 보관, 풀 반납, 터미널 종료, 대기열 스폰을 순서대로 한다. 30분 무응답이면 `sweep.sh` 가 같은 정리를 한다.
4. 종료된 스레드에 다시 쓰면 해피가 그 메시지로 새 마크를 띄운다. 같은 프로젝트에 마크가 이미 있으면 child worktree 로 뜬다.
5. 프라이데이 채널에서 "X 프로젝트에서 시작해" → 프라이데이가 직접 스레드를 열고 스폰한다.

상세 설계는 [SPEC.md](SPEC.md), 운영 명령과 절대 규칙은 [docs/ADMIN.md](docs/ADMIN.md).

## 구성

```
bin/                 스폰·종료·감시·상태 스크립트 (zsh, python3)
plugin/discord-orca/ Claude Code Discord 채널 플러그인 포크. 게이트 env 추가
                     (DISCORD_ONLY_CHATS, DISCORD_TOP_LEVEL_ONLY, DISCORD_THREAD_REGISTRY,
                      DISCORD_IGNORE_OTHER_BOT_MENTIONS, DISCORD_PRESENCE, DISCORD_ACTIVITY_FILE)
codex-worker/        비전 브리지. Discord 게이트웨이 ↔ codex app-server, at-most-once 전달 저널
jarvis/              자비스 데몬. 게이트 → 스레드 이력 + git 상태 → codex exec → 답변
roles/               역할 지침 (작업자·비전·자비스, 상담역 온디맨드 절차)
sessions/<역할>/     상시 세션 cwd. CLAUDE.md 가 역할 지침
templates/           진행 표시 훅 설정
launchd/             leads-check 5분, sweep 5분, 접수원 04:00 재시작, jarvis KeepAlive
routes.example.json  채널 → {name, path} 매핑과 봇·모델 설정 예시
```

## 전제 조건

- macOS. launchd, `caffeinate`, zsh 를 쓴다.
- Orca 앱과 `orca` CLI. 세션 터미널 생성·종료·출력 읽기를 Orca 에 위임한다.
- Claude Code CLI. 채널 플러그인과 `--dangerously-load-development-channels` 를 지원하는 버전.
- Codex CLI (`codex app-server`, `codex exec`) 와 `~/.codex/auth.json`.
- bun, python3.
- Discord 서버 하나와 봇 애플리케이션 8개 (프라이데이·해피·마크1~4·자비스·비전). Message Content intent 필요.

## 설치

스크립트와 역할 지침은 `~/orchestrator` 경로를 전제한다 (`ORCH_ROOT` 로 바꿀 수 있으나 지침 문서는 별도 수정).

```sh
git clone <this-repo> ~/orchestrator
for d in plugin/discord-orca codex-worker jarvis; do (cd ~/orchestrator/$d && bun install); done
```

봇 토큰은 봇 이름으로 된 env 파일에 둔다. git 밖이며 스크립트는 값을 stdout 에 내지 않는다.

```
~/.claude/channels/bots/<봇이름>.env
  DISCORD_BOT_TOKEN=...
  DISCORD_APP_ID=...
```

라우팅과 봇 설정:

```sh
cp routes.example.json routes.json   # 채널 ID·프로젝트 경로·길드·소유자 ID·상태 이모지 ID 를 채운다
claude plugin marketplace add ~/orchestrator/plugin
```

플러그인은 세션이 뜰 때 `bin/lib.sh ensure_plugin` 이 cwd 에 project 스코프로 설치한다. `--plugin-dir` 는 배너가 정상이어도 채널 메시지가 주입되지 않으므로 쓰지 않는다.

launchd:

```sh
cd ~/orchestrator/launchd && sed -i '' "s#__HOME__#$HOME#g" *.plist
for p in leads-check 접수원-restart sweep jarvis; do
  ln -sf ~/orchestrator/launchd/ai.orca.$p.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.orca.$p.plist
done
```

기동:

```sh
bin/lead-up.sh 상담역     # 프라이데이
bin/lead-up.sh 접수원     # 해피
bin/jarvis-up.sh          # 자비스. launchd 가 있으면 자동
bin/vision.sh fresh       # 비전
./lead.sh                 # 관리 세션. Discord 에 붙지 않는다
```

## 검증

```sh
(cd codex-worker && bun run typecheck && bun test)
(cd jarvis && bun run typecheck && bun test)
(cd plugin/discord-orca && bun test)
for f in bin/*.sh; do zsh -n "$f" || echo FAIL $f; done
bin/spawn-worker.sh <채널> <스레드> --title "…" --dry-run   # 풀·등록부를 건드리지 않는다
bin/status.sh
```

## 작업 로그

마크는 `runs/<project>/YYYY-MM-DD.md` 에 append 한다 (git 제외).

```
- HH:MM spawned 마크1 thread=<id> title="…" worktree=own
## HH:MM [<project>] thread_<id>
- 요청: …
- 결과: succeeded|failed — 한 줄 요약
- 파일: 수정 파일 목록
- HH:MM done 마크1 thread=<id>
```

자비스 검수 전문이 Discord 한도를 넘으면 `runs/<project>/jarvis-<날짜>-<스레드>-<턴>.md` 로 첨부한다.

## 보안

- 토큰은 `~/.claude/channels/bots/*.env` 에만 있다. 플러그인 `file-policy.ts` 가 봇 env, Codex 홈, `state/` 아래 파일의 첨부를 막는다.
- 마크는 `--dangerously-skip-permissions`, 비전은 `sandbox: danger-full-access` 로 돈다. 소유자만 있는 서버를 전제한다.
- 모든 게이트는 길드 일치 ∧ 소유자(또는 신뢰 봇) 작성 ∧ 허용 채널을 요구한다.

## 라이선스

Apache-2.0. `plugin/discord-orca/` 는 Anthropic 의 Claude Code Discord 채널 플러그인(Apache-2.0)을 수정한 것이며 변경 지점은 `orca fork` 주석으로 표시했다. [NOTICE](NOTICE) 참조.
