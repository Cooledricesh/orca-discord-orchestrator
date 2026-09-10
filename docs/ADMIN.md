# 관리 세션

이 폴더에서 `./lead.sh` 로 뜨는 세션은 **관리 세션**이다. Discord 에 붙지 않는다. 설계·스크립트·역할 지침을 고치고 검증하는 자리다. 구조는 `SPEC.md`.

## 절대 규칙
- **자기 자신을 죽이지 않는다.** 이 세션은 Orca 터미널의 `lead.sh` 프로세스다. `kill`, `pkill claude`, 핸들 없는 `orca terminal close` 전에 `ps -o pid,command -p $PPID` 로 확인한다.
- `orca terminal close/read/send` 는 항상 `--terminal <핸들>`.
- 봇 토큰(`~/.claude/channels/bots/*.env`)을 출력·커밋하지 않는다.
- 프로젝트 폴더는 직접 수정하지 않는다. 실작업은 Discord → 마크.
- zsh 에서 `path` 변수를 쓰지 않는다 (`PATH` 와 묶여 있다).

## 명령
| 명령 | 역할 |
|---|---|
| `bin/lead-up.sh <상담역\|접수원>` | 상시 세션을 Orca 터미널에 기동 (STATE_DIR·access.json 생성, 프롬프트 자동 수락). `DRY=1` 이면 명령만 출력 |
| `bin/leads-check.sh [--restart <역할>]` | 죽은 상시 세션 재기동, 살아 있는 상시 세션·마크에 플러그인 자식이 없으면 #운영-로그에 ⚠️ 1회(`state/plugin-missing/`), 켜 둔 비전이 죽어 있으면 `vision.sh resume` (launchd 5분). `--restart` 는 컨텍스트 비우기 |
| `bin/open-thread.sh <봇> <채널> <메시지id\|new> <이름\|@file> [<본문\|@file>]` | 스레드 생성 |
| `bin/spawn-worker.sh <채널> <스레드> [--title t\|@file] [--request-file f] [--request-message-id id] [--model m] [--effort e] [--new-worktree] [--resume] [--dry-run]` | 마크 스폰. exit 3 = 풀 꽉 참(queued) |
| `bin/finish-worker.sh <스레드> succeeded\|failed\|stopped` / `bin/stop-worker.sh <스레드>` | 마크 종료 / 강제 종료 |
| `bin/retry-queued.sh <스레드>` | queued 재시도 (finish 가 자동 호출) |
| `bin/sweep.sh` | 죽은 터미널·30분 무응답 마크 정리, 고아 lease 회수 (launchd 5분). 언제 돌려도 안전해야 한다 |
| `bin/pool.sh lease\|release\|status` | 마크 봇 풀 |
| `bin/status.sh` | 봇 상태 한 화면 (상시 세션·마크의 플러그인 자식, 풀, 대기열, 비전, 자비스). #운영-로그에 사용자가 쓰면 접수원이 실행해 올린다 |
| `bin/post-result.sh` | 마크 결과 카드 (소유자 멘션 포함) |
| `bin/vision.sh fresh [요청파일]\|resume\|attach\|stop\|status` | 비전. 실행 중이면 fresh/resume 전에 stop |
| `bin/jarvis-up.sh [--fg]` / `bin/jarvis-down.sh` | 자비스 (launchd KeepAlive 가 관리. 코드 반영은 `jarvis-down.sh` 만 하면 다시 뜬다) |

## 알아둘 것
- 플러그인은 공식 경로대로 세션 cwd 폴더에 project 스코프로 설치돼 뜬다 (`lib.sh ensure_plugin`, 저장소 `plugin/discord-orca/` 를 직접 실행). `--plugin-dir` 로는 배너가 깨끗해도 채널 메시지가 주입되지 않는다 (2026-09-10 실측, claude-code#43064). 같은 폴더에서 사용자가 직접 켠 claude 도 플러그인을 로드하지만 토큰이 없으면 `server.ts` 가 실패 대신 도구 없이 대기하므로, Claude Code 전역 15분 실패 캐시(`~/.claude/mcp-needs-auth-cache.json`)를 오염시키지 않는다 (09-09~10 장애 원인은 이 캐시였다). 수정 후 해당 세션을 재시작하면 반영된다.
- 같은 프로젝트 폴더에서 사용자가 직접 `claude` 를 켜도 된다 (플러그인 무관). 마크와 대화하려면 **떠 있는 마크 탭에 직접 타이핑**한다 (Discord 와 같은 세션). Orca 사이드바에서 마크 세션을 새로 열면 플러그인 없이 뜨므로 Discord 를 못 듣는다. 그 세션은 사용자 것이 되고 스레드는 sweep 이 ❌ 정리한다.
- 역할 지침(`sessions/*/CLAUDE.md`, `roles/*.md`)·env 변경은 세션 재시작 후 적용된다: 상담역·접수원은 `leads-check.sh --restart`, 마크는 다음 스폰부터, 비전은 `vision.sh stop && fresh|resume`, 자비스는 새 스레드부터.
- Discord 앱에서 스레드를 열면 채널처럼 보인다. 새 작업은 채널 최상위에 써야 새 스레드가 생긴다.
- 재부팅 후: launchd → 자비스 즉시, 상담역·접수원·비전(stop 하지 않았던 경우, 직전 세션 resume)은 leads-check 5분 틱. 마크는 복구되지 않는다 (sweep 이 ❌ 정리, 스레드에 다시 쓰면 재스폰).
- #운영-로그: 상시 세션 시작, 마크 배정·종료, 비전 fresh/resume/stop 이 한 줄씩 올라간다 (`lib.sh ops_log`).
- 세션이 떠도 Discord 플러그인 자식(`bun server.ts`)이 안 붙는 경우가 있다. lead-up·spawn-worker 가 기동 후 40초 안에 플러그인 자식과 배너(`plugin not installed` 없음)를 확인하고(`lib.sh plugin_ready`) 아니면 한 번 다시 띄운다. 그래도 없으면 스레드와 #운영-로그에 ⚠️ 를 올리고 출력 `plugin=missing` 으로 접수원에게 알린다 (접수원이 스레드에 경고). 살아 있는 세션의 플러그인 유무는 leads-check 5분 틱이 보고 상태가 바뀔 때만 알린다.
- 새 프로젝트: `roles/상담역-온보딩.md` (routes.json 추가 → 접수원 재시작).

## launchd
plist 는 `~/Library/LaunchAgents/` 에 **symlink** 로 둔다 (저장소 경로에서 직접 bootstrap 하면 재부팅 후 사라진다).
```
for p in leads-check 접수원-restart sweep jarvis; do
  ln -sf ~/orchestrator/launchd/ai.orca.$p.plist ~/Library/LaunchAgents/ai.orca.$p.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.orca.$p.plist
done
launchctl print gui/$(id -u)/ai.orca.sweep | head      # 상태
launchctl kickstart -k gui/$(id -u)/ai.orca.sweep      # 즉시 실행
launchctl bootout gui/$(id -u)/ai.orca.sweep           # 해제 (plist 수정 후 bootout → bootstrap)
```
로그: `state/log/`. 자비스는 `~/.claude/channels/bots/자비스.env` 와 `~/.codex/auth.json` 이 있어야 뜬다.

## 검증
```
(cd codex-worker && bun run typecheck && bun test)
(cd jarvis && bun run typecheck && bun test)
(cd plugin/discord-orca && bun test)
for f in bin/*.sh; do zsh -n "$f" || echo FAIL $f; done
./bin/spawn-worker.sh <채널> <스레드> --title "…" --dry-run     # 풀·등록부를 건드리지 않는다
DRY=1 ./bin/lead-up.sh 접수원
```
실사용 점검: 프로젝트 채널 최상위에 메시지 → 스레드·마크 생성 → 후속 질문 → "종료". 잔여물은 `pool.sh status`, `ls state/threads`, `orca terminal list --json`, `pgrep -fl 'bun server.ts'`.
