# Grok 엔진 작업자 — 설계 + 스파이크 (1단계)

상태: 설계안. 사용자 확인 후 2단계 구현. 스레드 1553727866007724083.

## 0. 요약

- 마크(Claude)와 같은 흐름(채널 요청 → 스레드 → 전용 worktree → 진행 표시 → 결과 → 정리)을 따르고, 프로세스만 **브리지(`grok-worker/bridge.ts`)**로 바꾼다. 브리지는 Discord 게이트웨이로 자기 스레드의 소유자 메시지만 받고, 사용자 메시지 하나당 `grok -p … --resume <sid>`를 한 번 실행한다(턴 = 프로세스 1개).
- 등록부·풀·잠금·pid·activity·worktree·finish/stop/sweep는 기존 것을 쓴다. 엔진에 따라 달라지는 곳은 **실행 커맨드, 종료 시 kill 대상, 상태 판정, 대기열 선택** 네 군데뿐이다.
- 스파이크에서 파일 수정·셸·streaming-json·`--resume`·`usage`가 모두 동작했다. 대신 **설계를 바꿔야 하는 발견이 하나** 있었다. grok은 기본적으로 `~/.claude`의 플러그인·MCP·훅·스킬·CLAUDE.md를 불러오고, 여기에는 **Claude Discord 플러그인 MCP 서버**도 들어 있다. 개인 봇 게이트웨이에 중복으로 접속하게 된다. 해결책은 grok 프로세스에만 **HOME을 격리**하는 것이다(§4.3, 스파이크 7에서 검증).

## 1. 스파이크 결과 (grok 1.0.41, 임시 git repo, 2026-09-27)

| # | 확인 | 결과 |
|---|---|---|
| 1 | `-p` + `--cwd` + `--session-id` + `--system-prompt-override` + `--output-format streaming-json` + `--always-approve` 조합으로 파일 수정과 셸 실행 | ✅ `search_replace`로 a.txt 수정, `run_terminal_command`로 `echo SHELL_OK > shell.txt && pwd`, pwd = cwd. 33초, rc=0 |
| 2 | 출력 이벤트 형식 | ✅ NDJSON, 줄마다 `type` 필드: `available_commands`, `thought`(델타), `text`(델타), `tool_call`, `tool_call_update`, `usage`(모델 호출마다), `end`. 아래 예시 참고 |
| 3 | `--resume <sid>`로 이어 쓰기 | ✅ 직전 턴 내용을 기억("직전에 넣은 줄은 `world`")하고 추가로 수정. `end.sessionId` 동일. `--session-id`는 **새 세션 전용**이라 이미 있는 id를 주면 오류가 난다. 재개는 반드시 `--resume` |
| 4 | `grok usage <sid>` | ✅ JSON: `session.{inputTokens,outputTokens,cachedReadTokens,reasoningTokens,totalTokens,modelCalls,costUsdTicks,turnCount,primaryModelId}` + `turns[]`. `costUsdTicks / 1e10 = USD`(`end.total_cost_usd`와 일치). 2턴 합계 176k 토큰, $0.049 |
| 5 | 없는 세션 `--resume` | rc=1, stderr `Session "…" not found locally, restoring conversation from remote… 404 Not Found`. 새 세션으로 조용히 바뀌지는 않는다 |
| 6 | `--sandbox workspace` | ✅ cwd 안 쓰기 rc=0, `$HOME/…` 쓰기 rc=1 `operation not permitted`. 단 **운영 스크립트가 쓰는 `$ORCH_ROOT/state`·`runs`·`~/.claude/channels/workers`도 막힌다**(§4.4) |
| 7 | 격리 HOME + `--rules`(시스템 프롬프트 뒤에 덧붙임) | ✅ 플러그인 0, MCP 0, CLAUDE.md 0, grok.com 로그인 유지. 역할 규칙을 따랐고("그록1"), `~/.gitconfig` 심볼릭 링크로 git 사용자 정보 확인. **9.5초**, $0.016 |
| ✗ | 격리하지 않은 상태(스파이크 6) | `grok inspect`: Plugins 15(discord·telegram MCP, oh-my-fable·superpowers 훅 포함), Hooks 25, Claude 스킬 80여 개. 모델이 "Discord 응답 도구를 찾겠다"며 `~/.claude/plugins/.../discord`와 `~/.claude/channels/discord/access.json`을 뒤졌다(토큰 파일은 읽지 않았다). `GROK_CLAUDE_*_ENABLED=false`로는 **플러그인이 꺼지지 않고**, `GROK_CONFIG` 오버레이는 허용 목록 밖이라 `plugins`·`compat`가 무시된다 |

이벤트 예시(요약):
```json
{"type":"tool_call","toolCallId":"call-…-3","title":"run_terminal_command","kind":"execute","status":"pending","toolName":"run_terminal_command","rawInput":{"command":"echo SHELL_OK > shell.txt && pwd","description":"…"}}
{"type":"tool_call","toolCallId":"call-…-2","toolName":"search_replace","kind":"edit","rawInput":{"file_path":"/…/a.txt","old_string":"hello","new_string":"hello\nworld"}}
{"type":"tool_call_update","toolCallId":"call-…-3","status":"completed","rawOutput":{"type":"Bash","exit_code":0,…}}
{"type":"text","data":"a"}
{"type":"end","stopReason":"end_turn","sessionId":"60ddbd58-…","usage":{"input_tokens":28512,"output_tokens":1887,"reasoning_tokens":1269,"total_tokens":116415},"num_turns":4,"total_cost_usd":0.03786036,"modelUsage":{"grok-4.7-build":{…}}}
```
관찰:
- `text` 델타에는 **도구 호출 전의 짧은 서술**("두 명령을 실행하고 확인하겠습니다.")이 붙어 있다. 사용자에게 보낼 답은 **마지막 `tool_call` 이후에 나온 text**만 쓴다.
- 모델 id `grok-4.7`은 과금상 `grok-4.7-build`로 기록된다.
- `tool_call.toolName`은 `run_terminal_command | read_file | search_replace | write | list_dir | grep | spawn_subagent | web_search | web_fetch | …`이다. 진행 표시는 이 값과 `rawInput`으로 만든다.
- 종료 코드: 0 성공, 1 오류(인증·네트워크·런타임), 130 SIGINT, 143 SIGTERM (headless 문서).

## 2. 등록 스키마 (`routes.json`)

```jsonc
"bots": {
  "workers": [
    "마크1",                                              // 문자열 = {name, engine:"claude"} (하위 호환)
    {"name": "그록1", "engine": "grok"},
    {"name": "그록2", "engine": "grok", "model": "grok-4.7-build-fast", "effort": "high"}
  ]
},
"models": { "작업자": "opus", "작업자Effort": "medium", "grok작업자": "grok-4.7", "grok작업자Effort": "" }
```
- 모델 결정 순서: spawn `--model` > 봇 항목 `model` > 엔진 기본값(`작업자` / `grok작업자`). effort도 같다. 빈 값이면 CLI 기본값을 쓴다.
- `engine`은 `claude | grok`만 허용한다. 모르는 엔진, 이름 중복, 빈 이름은 doctor가 오류로 처리한다.

## 3. 파일별 변경 목록

### Python/zsh (운영 경로)
| 파일 | 변경 |
|---|---|
| `bin/config.py` | `ENGINES`, `worker_entries(data)` → `[{name, engine, model, effort}]`(문자열은 claude로 정규화). `load_routes` 검증(항목은 str 또는 `{name:str, engine∈ENGINES}`). `enabled_bots`는 이름만 쓴다. CLI `config.py workers [engine]` → `name\tengine\tmodel\teffort` 줄 출력 (셸용) |
| `bin/lib.sh` | `worker_bots [engine]`는 `config.py workers`를 쓴다. `worker_engine <bot>`, `registry_engine <tid>`(없으면 claude) 추가 |
| `bin/pool.sh` | `lease <tid> [engine]`: 그 엔진의 봇에서만 고른다. `status`에 엔진 열을 추가한다(`그록1  grok  <tid>  <since>`). 첫 줄 합계는 그대로 두고 엔진별 합계를 붙인다 |
| `bin/spawn-worker.sh` | `--engine claude\|grok`, `--bot <name>`(엔진은 그 봇의 엔진, 그 봇만 lease) 추가. 기본 claude. lease를 엔진으로 거르고, 꽉 차면 queued 레코드에 `engine`(과 `bot`)을 저장한다. 모델 기본값은 엔진별. **7단계(실행 커맨드)와 9단계(기동 확인)만 분기**: claude는 현행 그대로, grok은 `exec bun grok-worker/bridge.ts <registry>`이고 `ensure_plugin`·`accept_prompts`·`plugin_ready` 대신 브리지 ready(런타임 파일) 대기. 등록부에 `engine` 기록 |
| `bin/retry-queued.sh` | queued 레코드의 `engine`/`bot`을 `--engine`/`--bot`으로 전달 |
| `bin/finish-worker.sh` | kill 대상은 `pid_is "$pid" claude` 대신 엔진별 패턴(grok은 `grok-worker/bridge.ts`, 브리지가 자식 grok 프로세스 그룹을 정리). 대기열은 **반납된 봇과 같은 엔진**의 queued 중 첫 건만 스폰. grok이면 `grok usage` 한 줄을 runs 로그에 남긴다 |
| `bin/stop-worker.sh`, `bin/sweep.sh` | 변경 없음. 브리지가 메시지를 받을 때 activity 파일을 갱신하고, 턴 중에는 터미널에 로그를 찍으므로 `lastOutputAt`이 갱신된다 |
| `bin/status.sh` | 작업자 줄은 엔진별로 판정한다. grok은 pid(`bridge.ts`) + 런타임 파일 `status`(ready/auth_failed 등)를 보고 `✅/⛔/❌`를 표시하고 엔진 표기(`그록1 [grok]`)를 붙인다 |
| `bin/post-result.sh` | 등록부 `engine=grok`이면 카드 본문 끝에 `grok usage` 한 줄(`토큰 in 174k · out 2.2k · $0.049 · grok-4.7-build`) 추가. 실패해도 카드는 보낸다 |
| `bin/setup.py` | 스키마 검증은 `config.worker_entries`로 한다. `doctor`: grok 작업자가 있으면 `grok` 실행 파일과 `grok models` 첫 줄의 로그인 상태, `bun`, `grok-worker/node_modules`를 확인한다(토큰·auth.json은 읽지 않는다). `bot-env`/`invites`는 이름 기반으로 동작하며 권한은 작업자와 같다 |
| `bin/model_control.py` | 하드코딩된 `ROLES` 중 작업자 봇은 `worker_entries`에서 만든다. grok 봇의 `!모델 그록1 <m> [effort]`는 `grok models` 목록 안의 모델만 허용하고 `models.grok작업자(Effort)`를 바꾼다(마크가 `작업자`를 바꾸는 것과 대칭, 다음 새 작업부터 적용). Claude 봇에 grok 모델, grok 봇에 Claude 모델은 거부 |
| `bin/operations.py` | `notify()`의 `allowed`가 `workers`를 그대로 펼친다. dict 항목이 되면 **모든 작업자 봇의 결과 통지가 조용히 거부된다**. `worker_entries` 이름으로 교체 |
| `sessions/접수원/CLAUDE.md` | "사용자가 '그록으로/Grok으로'라고 하면 `--engine grok`" 추가. exit 3 안내는 엔진을 명시(`⌛ Grok 작업자 전부 사용 중 — 대기열`). 기본은 Claude |
| `bin/worker-discord.sh` (신규) | `react <messageId> <emoji>`만 제공. 봇은 등록부의 bot, 채널은 `ORCA_THREAD_ID`로 고정. grok 세션이 셸로 부른다 |
| `roles/작업자-grok.md` (신규) | `작업자.md`에서 입출력 규칙만 바꾼 grok용 역할 문서(§4.2). `작업자.md`는 건드리지 않는다(마크 동작 불변) |
| `routes.example.json` | `workers` 예시에 grok 항목(주석), `grok작업자` 기본값 |

### TypeScript
| 파일 | 내용 |
|---|---|
| `bridge-kit/progress.ts` (추출) | `codex-worker/service.ts`의 진행 카드(`⏳ N · …`, 5초 스로틀 edit, 턴 끝 delete, 재시작 시 잔여 카드 삭제)를 클래스로 만든다. 비전과 grok 브리지가 같이 쓴다 |
| `bridge-kit/discord-io.ts` (추출) | `.env` 토큰 읽기, 첨부 다운로드(25MB, CDN 호스트 검증), `assertFile`, 1900자 분할 전송. `codex-worker/discord.ts`는 이 모듈을 호출하도록 바꾼다(동작 동일, `discord.test.ts`·`core.test.ts`·`tsc` 통과 조건) |
| `grok-worker/stream.ts` | NDJSON 파서(순수 함수). 줄 → `{kind:'tool', name, desc}` / `{kind:'text'}` / `{kind:'end', sessionId, usage, cost}` / `{kind:'bad', line}`. 최종 답은 마지막 tool_call 이후 text. 진행 설명은 `progress-hook.py describe()`와 같은 형식: `run_terminal_command` → `` `cmd` ``, `read_file/search_replace/write` → 상대경로, `grep` → 패턴, `spawn_subagent` → `↳ 서브 에이전트 …`, `web_*` → url/query |
| `grok-worker/args.ts` | `grok` 인자 빌더(순수 함수, 테스트 대상). §4.1 |
| `grok-worker/home.ts` | 격리 HOME 생성(§4.3) |
| `grok-worker/bridge.ts` | 수명·게이트웨이·턴 루프(§5) |
| `grok-worker/{stream,args}.test.ts`, `package.json` | bun test. discord.js 의존 |

### 테스트 (기존 관례: `tests/test_runtime.py` unittest + 가짜 orca, `*.test.ts` bun)
- config: 문자열·dict 혼합 `workers` 파싱, 문자열만 있는 기존 routes에서 동작 불변, 잘못된 engine은 doctor 오류.
- pool: `lease <tid> grok`은 grok 봇만 준다. grok 풀이 꽉 차면 claude 봇이 비어 있어도 exit 3.
- spawn `--dry-run --engine grok`: 커맨드에 `bridge.ts` 포함, `--dangerously-skip-permissions`·플러그인 인자 없음, 등록부에 engine. `--engine` 없는 dry-run 출력은 **기존과 바이트 단위로 같음**(회귀).
- finish: 반납된 grok 봇은 queued claude 항목을 스폰하지 않는다.
- operations.notify: dict 항목 봇도 허용.
- 브리지 파서: 스파이크 NDJSON 픽스처(도구 전 서술 제거, end 없음 = 파싱 실패, 비JSON 줄 무시).
- 비전 회귀: `codex-worker` `bun test` + `tsc --noEmit`.

## 4. 실행 어댑터 (grok)

### 4.1 턴 실행 커맨드
```
HOME=<격리HOME> GROK_HOME=<실제 ~/.grok> ORCH_ROOT=… ORCA_THREAD_ID=<tid> …(runtime_exports)
grok -p "<입력>" --cwd <worktree> \
     (--session-id <sid> | --resume <sid>) \
     --rules "<roles/작업자-grok.md + 기동 정보>" \
     --output-format streaming-json --always-approve \
     --model <model> [--reasoning-effort <effort>] \
     --no-plan --disallowed-tools ask_user_question
```
- 첫 턴(새 작업): `--session-id <spawn이 만든 sid>`. 이후 턴과 `--resume` 재개는 `--resume <sid>`. 브리지 런타임 파일의 `sessionStarted`로 구분한다(첫 턴이 세션 생성 전에 실패하면 다시 `--session-id`).
- 역할 주입은 `--system-prompt-override`가 아니라 **`--rules`(덧붙이기)**로 한다. override는 grok 기본 에이전트 프롬프트(도구 사용법)를 통째로 대체한다. 스파이크 1에서는 문제가 없었지만 긴 작업에서 도구 품질이 떨어질 위험이 있다. 스파이크 7에서 `--rules`가 규칙을 지켰다. 입력 텍스트가 길 수 있으므로 첫 턴 프롬프트는 `--prompt-file`로 넘긴다.
- 권한: `--always-approve`는 마크의 `--dangerously-skip-permissions`와 같은 수준이다(deny 규칙과 훅은 유지). `ask_user_question`·plan 모드는 headless에서 대기 없이 실패하므로 끈다(마크 금지 사항과 같음).
- 서브에이전트(`spawn_subagent`)는 켜 둔다(마크의 Agent 위임에 대응).

### 4.2 역할 문서 `roles/작업자-grok.md` (작업자.md와 다른 부분만)
- 입력은 사용자 메시지 원문이 턴 프롬프트로 온다(`<channel …>` 태그 없음). 첨부는 브리지가 받아 `첨부: <절대경로>` 줄로 붙인다.
- **턴의 마지막 답변 텍스트를 브리지가 스레드에 올린다.** reply 도구는 없다. 진행·잡담 서술을 쓰지 않는다. 질문도 최종 텍스트로 쓴다.
- 결과 카드(`post-result.sh`)와 runs 로그 append는 마크와 같이 셸로 호출한다. `react`는 `bin/worker-discord.sh react <messageId> <emoji>`(신규 소형 헬퍼, 등록부의 봇 토큰, 자기 스레드 고정)로 한다.
- 자비스 검수 브리프는 최종 텍스트에 `<@jarvisAppId>`를 쓰면 된다. 브리지는 소유자와 jarvisAppId 멘션만 허용하고 `@everyone/@here`는 막는다.
- **"종료"·"중단"은 브리지가 처리한다.** 모델은 finish를 호출하지 않는다. finish는 터미널을 닫으므로 모델이 호출하면 턴 결과 전송 전에 브리지가 죽는다.
- 나머지(작업 경로, 인계, 운영-적용, 금지 사항)는 작업자.md와 같은 문장을 쓴다. 공통 부분이 어긋나지 않게 하는 테스트를 하나 둔다(두 파일의 공통 절 비교).

### 4.3 HOME 격리 (필수)
- 문제: grok은 `~/.claude/settings.json`의 `enabledPlugins`를 따라 Claude 플러그인을 켠다(discord·telegram MCP, 훅 포함). 이를 끄는 **프로세스 단위 스위치가 없다**(`GROK_CLAUDE_*` env는 스킬·규칙·훅·MCP 파일 스캔만 끄고 플러그인은 못 끈다. `GROK_CONFIG` 오버레이는 `plugins`/`compat`를 무시한다). `grok plugin disable`은 사용자 전역 설정을 바꾸므로 쓰지 않는다.
- 방법: `state/threads/<tid>.grok/home/`에 **실제 HOME의 최상위 항목을 심볼릭 링크로 미러링하되 `.claude`, `.claude.json`, `.cursor`, `.codex`, `.grok`는 제외**한다. `GROK_HOME=<실제 ~/.grok>`은 절대경로로 따로 준다. 이렇게 하면 git·gh·orca·ssh 등 HOME 기반 도구는 그대로 동작하고 grok 발견 경로에서만 Claude 설정이 빠진다(스파이크 7: 플러그인 0, MCP 0, 로그인 유지, gitconfig 동작). grok 셸 도구의 자식도 같은 HOME을 쓰지만 운영 스크립트는 `runtime_exports`의 절대경로(`BOTS_DIR` 등)를 쓰므로 영향이 없다.
- 남는 것: `~/.grok/hooks/orca-status.json`(Orca 앱의 상태 표시 훅)은 grok 자체 훅이라 유지된다. Orca 터미널 상태 표시에 쓰이며 무해하다.
- 격리 HOME 생성에 실패하면 브리지는 grok을 실행하지 않고 `failed`로 끝낸다(격리 없이 실행하지 않는다).

### 4.4 worktree 밖 쓰기 차단 (선택, 기본 끔)
- `--sandbox workspace`는 worktree 밖 쓰기를 커널 수준에서 막는다(스파이크 6). 하지만 `post-result.sh`는 쓰지 않아도, 모델이 부르는 runs 로그 append와 운영-적용(`operations.py`)은 `$ORCH_ROOT` 아래에 쓴다. 사용자 정의 프로필(`extends="workspace"`, `read_write=[$ORCH_ROOT/runs, $STATE_DIR_ROOT]`)은 `~/.grok/sandbox.toml`이나 프로젝트 `.grok/sandbox.toml`에만 둘 수 있다. 전자는 사용자 전역 파일이고 후자는 대상 worktree를 오염시킨다.
- 제안: 기본은 **마크와 같은 수준(샌드박스 없음, 프롬프트 규칙)**으로 둔다. `routes.json` `grokSandbox: "<프로필명>"`을 두면 `--sandbox <이름>`을 붙이고, 프로필 스니펫은 `setup.py doctor`가 안내만 한다(사용자가 직접 `~/.grok/sandbox.toml`에 추가). 오케스트레이터 자체 작업(운영-적용)은 샌드박스에서 동작하지 않으므로 그 경우 끈다.

## 5. 브리지 프로세스 수명 (`grok-worker/bridge.ts <state/threads/<tid>.json>`)

```
spawn-worker.sh ─ orca terminal create ─ zsh: print $$ > <tid>.pid; exec bun bridge.ts <registry>
  bridge ── discord.js 게이트웨이 (봇 = 등록부 bot, 토큰 = STATE_DIR/.env)
         ├─ ready → state/threads/<tid>.grok/runtime.json {pid, status:'ready', sessionStarted}
         ├─ 첫 턴: 기동 프롬프트 파일 (현행 <tid>.prompt.md 와 같은 내용)
         └─ 턴마다 child: grok -p … (자기 프로세스 그룹, stdout NDJSON → 파서)
finish-worker.sh ─ 등록부·풀·STATE_DIR 정리 → orca terminal close → pid(bridge.ts) TERM/KILL
  bridge SIGTERM → grok 그룹 TERM → 게이트웨이 종료
```
- **수신 규칙**(Claude 플러그인 `DISCORD_ONLY_CHATS`·`IGNORE_OTHER_BOT_MENTIONS`와 같다): guild 일치, `channelId == threadId`, 작성자 = ownerUserId, 봇·웹훅 무시, 다른 봇만 멘션한 메시지(`@자비스 …`) 무시(`mentionsOtherBotOnly` 재사용). 받으면 `emojis.working` 반응, activity 파일 갱신.
- **턴 직렬화**: 한 번에 grok 하나만 돈다. 턴 중에 온 메시지는 모았다가 턴이 끝나면 한 번에 다음 턴으로 넘긴다(grok `-p`는 턴 중 steer가 안 된다). 모인 메시지가 있으면 진행 카드 끝에 `(+N 대기)`를 붙인다.
- **진행 표시**: `tool_call` 이벤트마다 `⏳ N · <설명>`. 첫 호출은 POST, 이후는 5초 스로틀로 edit, 턴 끝에 delete. 턴 중에는 8초마다 typing. 형식과 빈도는 `progress-hook.py`와 같다(`bridge-kit/progress.ts`).
- **답 게시**: `end`를 받으면 마지막 tool_call 이후 텍스트를 1900자로 나눠 게시한다(멘션은 소유자·jarvisAppId만). 빈 텍스트면 게시하지 않는다(모델이 post-result 카드만 올린 경우).
- **종료 키워드**: 메시지가 정확히 `종료`면 `⏹ 세션 종료` 게시 → `finish-worker.sh <tid> succeeded`를 **분리 프로세스**(nohup)로 실행한다. `중단`이면 진행 중인 grok에 SIGINT → 상태 2줄(마지막 도구·경과) → finish `stopped`.
- **등록부 갱신**: `engine=grok`, `sessionId`, `lastActivityAt`(턴 종료와 메시지 수신 때), `lastTurn={exit, costUsd, tokens}`.
- **로그**: 턴마다 원본 NDJSON·stderr를 `state/threads/<tid>.grok/turn-<n>.{ndjson,err}`에 저장(최근 5개 유지). 터미널에는 한 줄 요약(도구명)만 찍는다(sweep의 `lastOutputAt` 근거).
- **재개(`spawn --resume`)**: 등록부 `sessionId`와 `path`를 그대로 쓰고 첫 턴부터 `--resume`. 마크와 같이 사용자가 명시했을 때만.

## 6. 실패 모드

| 상황 | 감지 | 동작 |
|---|---|---|
| grok 로그인 만료/인증 실패 | exit 1 + stderr의 `login`/`auth`/`401` 계열 | 진행 카드 삭제, 스레드에 `⛔ Grok 로그인이 만료됐습니다. 관리 터미널에서 \`grok login\` 후 같은 메시지를 다시 보내세요.`, `ops_log` 한 줄, runtime `status=auth_failed`(status.sh ⛔). 브리지는 살아 있고 다음 메시지로 재시도한다. 자동 재로그인은 하지 않는다 |
| 세션 재개 실패 | `--resume` exit 1 + `not found` | 대화 맥락이 없다고 스레드에 알리고 **새 sid로 새 세션**을 만든다. 기동 정보를 다시 주입하고 방금 메시지를 이어 붙인다. 등록부 `sessionId` 교체, `previousSessionIds`에 이전 값 보관. 조용히 넘어가지 않는다 |
| 출력 파싱 실패 | 비JSON 줄(무시하고 카운트), exit 0인데 `end` 없음, `{"type":"error"}` | `⚠️ Grok 응답을 해석하지 못했습니다 (exit N). 로그: <turn-n.ndjson>`. 세션은 유지. 텍스트 델타가 있으면 그 텍스트를 게시한다 |
| grok 비정상 종료(1, 신호) | exit code | `❌ Grok 실행 실패 (exit N): <stderr 마지막 줄, 토큰류 마스킹>`. 연속 3회면 ops_log에 남긴다 |
| 턴 무응답(행) | 출력이 60분 동안 없음 | grok 그룹 TERM, `⚠️ 60분 동안 출력이 없어 중단했습니다` |
| 격리 HOME 생성 실패 | 브리지 시작 시 | ready로 가지 않고 `failed`로 끝남. spawn-worker가 스레드에 ⚠️ 게시, 풀 반납 |
| Discord 게이트웨이 끊김 | discord.js | 자동 재연결. 5분 넘게 연결되지 않으면 runtime `status=degraded`(status.sh ⚠️) |
| 브리지 크래시 | 터미널 종료 | sweep이 터미널 없음 → `finish failed`(현행 경로) |
| 봇 토큰/MessageContent intent 없음 | 로그인 실패 | spawn이 ready 대기 타임아웃 → 스레드 ⚠️ + 풀 반납(마크의 plugin missing과 같은 자리) |

## 7. 관측
- `pool.sh status`: `그록1  grok  <tid>  <since>`, 엔진별 합계.
- `status.sh`: `✅ 그록1 [grok] — project · title`, 로그인 만료는 ⛔.
- 결과 카드 끝의 한 줄: `grok usage` → `토큰 입력 174k(캐시 145k) · 출력 2.2k · 추론 1.4k · $0.049 · grok-4.7-build · 2턴`. grok.com 구독 인증에서 `$`는 API 환산값이며 실제 청구액이 아니다. 카드에 `(환산)`으로 표기한다.

## 8. 문서
- `SPEC.md`: 1절 엔진 문장과 역할 표에 "작업자(Grok) — 배정 스레드의 소유자 입력 — 스레드별 grok 세션(브리지) + 별도 worktree" 추가. 봇 하나 = 엔진 하나 원칙 유지(엔진은 봇 항목에 고정, `!모델`로 엔진은 못 바꾼다).
- `docs/ADMIN.md`: grok 봇 추가 절차(Discord 앱 생성 → `setup.py bot-env 그록1` → routes `workers`에 `{"name":"그록1","engine":"grok"}` → `setup.py invites` → `doctor`), 전제 조건 `grok login`(grok.com) 상태, `cd grok-worker && bun install`, 로그인 만료 시 대응.
- `docs/SETUP.ko.md`: 선택 구성으로 같은 절차 요약.

## 9. 결정이 필요한 것 (2단계 전)
1. **HOME 격리 방식**(§4.3) 채택 여부. 대안은 `grok plugin disable discord telegram …`로 전역 설정을 바꾸는 것이다(대화형 grok에도 적용된다). 권장은 격리.
2. **샌드박스 기본 끔**(§4.4, 마크와 같은 수준) + 선택 프로필. 또는 기본 켬(사용자가 `~/.grok/sandbox.toml`에 프로필 추가 필요).
3. **"종료"/"중단"을 브리지가 처리**(모델은 finish를 부르지 않는다). 마크와 사용자 체감은 같다.
4. 턴 중 메시지는 **모아서 다음 턴**으로(steer 없음). 즉시 끼어들기가 필요하면 `grok agent stdio`(ACP) 상주 방식으로 바꿔야 한다. 복잡도가 커지므로 2단계 범위에서는 제외를 권장.
5. 2단계에는 Grok 봇 앱(그록1)의 Discord 토큰이 필요하다: `python3 bin/setup.py bot-env 그록1` (MessageContent intent 켜기).
