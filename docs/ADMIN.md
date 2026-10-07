# 운영 가이드

저장소 폴더에서 실행한다. `./lead.sh`는 Discord에 연결하지 않는 관리용 Claude 세션이다. 자기 관리 터미널을 종료하지 않도록 Orca 명령은 항상 정확한 핸들을 사용한다. 봇 env와 인증 파일은 출력·커밋하지 않는다.

## 명령

| 명령 | 동작 |
|---|---|
| `python3 bin/setup.py doctor [--offline] [--json]` | 읽기 전용 준비 상태 점검 |
| `python3 bin/setup.py bot-env <봇>` | 직접 연 터미널에서 앱 ID·토큰 저장 |
| `python3 bin/setup.py invites` | 활성 봇별 초대 URL 출력 |
| `bin/lead-up.sh 상담역` / `접수원` | 상시 Claude 봇 기동. `DRY=1`이면 실행 내용만 출력 |
| `bin/leads-check.sh [--restart <역할>]` | 활성 상시 역할 복구, 플러그인 누락 감시. restart는 새 컨텍스트 |
| `bin/open-thread.sh <봇> <채널> <메시지ID 또는 new> <제목 또는 @file> [본문 또는 @file]` | Discord 스레드 생성 |
| `bin/spawn-worker.sh <채널> <스레드> [--title t 또는 @file] [--request-file f] [--engine claude 또는 grok] [--bot <봇>] [--resume] [--dry-run]` | 작업 전용 worktree 스폰. 그 엔진 풀이 차면 exit 3 (queued) |
| `bin/finish-worker.sh <스레드> succeeded 또는 failed 또는 stopped` | 작업 정리·봇 반환·대기열 재시도. 새 worktree 작업은 그 worktree·하위 worktree 터미널을 모두 닫는다 |
| `bin/stop-worker.sh <스레드>` | stopped로 정리 |
| `bin/retry-queued.sh <스레드>` | 대기 작업 재시도 |
| `bin/sweep.sh` | 종료된 터미널·30분 이상 유휴 작업 정리·끝난 스레드 worktree 에 남은 터미널 닫기·worktree 자동 정리(1시간마다)와 주간 보고·고아 lease 회수 |
| `bin/worktrees.py report [--json]` / `prune --dry-run` | 작업 worktree 목록(상태·용량·미커밋·운영 HEAD 미병합·보존 파일·경과일·터미널 수) / 자동 정리 대상 미리보기 |
| `bin/pool.sh status` / `bin/status.sh` | 풀(봇·엔진·스레드) / 전체 상태 확인 |
| `bin/jarvis-up.sh [--fg]` | 자비스 기동. --fg는 launchd용 |
| `bin/jarvis-down.sh` / `bin/jarvis-restart.sh` | 자비스 종료 / 설정을 다시 읽고 재시작 |
| `bin/vision.sh fresh 또는 resume 또는 attach 또는 stop 또는 status` | 활성화했을 때만 쓰는 비전 제어 |

작업자가 종료 스크립트를 자기 세션에서 실행할 때는 Discord 답장을 모두 마친 후 마지막 행동으로 부른다. 종료 후 `pool.sh status`로 반환을 확인한다. 종료 중 프로세스가 끊겨 상태가 남으면 관리 세션의 `sweep.sh`로 회수 상태를 확인한다.

## 설정 변경

### Discord에서 운영 적용

프라이데이 또는 오케스트레이터 프로젝트의 마크가 [운영 적용 절차](../roles/운영-적용.md)를 따른다. 소유자가 운영 장애 해결을 요청하면 적용까지 수행하며, 검토만 요청했을 때는 적용하지 않는다. 임의 셸 문자열을 실행하는 Discord 명령은 추가하지 않는다.

```sh
python3 bin/operations.py plan --thread <작업스레드ID>
python3 bin/operations.py plan --restart 상담역 --chat <결과를받을채널ID>
python3 bin/operations.py plan --launchd sweep --chat <결과를받을채널ID>
python3 bin/operations.py apply <계획ID>
python3 bin/operations.py status [계획ID]
python3 bin/operations.py retry <실패한계획ID>
```

계획은 운영·작업 커밋, 브랜치, 설정 지문과 적용 대상을 고정한다. dirty checkout, 다른 프로젝트, 분기된 커밋은 거부한다. 작업 worktree에서 전체 Python/Bun 테스트와 타입/셸 검사를 통과하고 상태를 재확인한 뒤 운영 브랜치를 fast-forward한다. `refs/operations/<id>`에 이전 커밋을 남긴다. 자동 reset/강제 병합/작업 폴더 삭제는 하지 않는다.

실행기는 별도 프로세스와 저장된 코드 복사본으로 동작하므로 자기 재시작이나 운영 코드 교체로 끊기지 않는다. `state/operations/<id>/job.json`에 단계·반영 여부·결과, `apply.log`에 검증 로그, `backup/`에 이전 plist를 저장한다. 중복 적용은 거부한다. 코드 반영 후 재시작 실패는 `promoted: true, status: failed`이며 같은 계획을 retry하면 완료 단계를 반복하지 않는다. 커밋이나 설정이 달라졌으면 새 계획이 필요하다. 코드 롤백은 기록된 이전 커밋을 참고해 revert 커밋으로 만들어 같은 절차로 적용한다.

launchd는 지정한 기존 job만 재등록하고 설치된 환경변수를 보존한다. 실행 중인 sweep/감시 job은 중단하지 않는다. 재등록 검증 실패 시 기존 plist와 등록 복원을 시도한다. `--chat` 또는 작업 스레드가 있으면 결과를 그 대화에 보고하며, 전송 실패도 `reported: false`로 남는다. 상태 확인은 모델 응답·사용량 복구까지 보장하지 않는다. 기존 마크는 강제 재시작하지 않고 새 작업부터 갱신된 지침/플러그인을 사용한다.

### 그록 작업자 추가 (선택)

Grok 엔진 작업자는 마크와 같은 스레드·worktree·풀 흐름을 쓰고 프로세스만 `grok-worker/bridge.ts`로 바뀐다 ([설계](grok-worker.md)).

전제 조건:
- 관리 터미널에서 `grok login`(grok.com) 완료. `grok models` 첫 줄이 `You are logged in …`이어야 한다. 인증 파일 내용은 출력하지 않는다.
- `cd grok-worker && bun install --ignore-scripts`

절차:
1. Discord Developer Portal에서 새 앱(예: 그록1)을 만들고 Bot의 **Message Content Intent**를 켠다.
2. `routes.json`의 `bots.workers`에 `{"name": "그록1", "engine": "grok"}`를 추가한다. 봇별 `model`·`effort`를 줄 수 있다. 기본값은 `models.grok작업자`·`models.grok작업자Effort`.
3. `python3 bin/setup.py bot-env 그록1` (직접 연 터미널)
4. `python3 bin/setup.py invites`로 받은 URL로 초대한다. 권한은 마크와 같다.
5. `python3 bin/setup.py doctor`로 grok 실행 파일·로그인·bun·`grok-worker/node_modules`를 확인한다.
6. 새 작업 봇 목록은 접수원 재시작 없이 다음 스폰부터 적용된다. 자비스가 그록의 검수 브리프를 신뢰하려면 `jarvis-restart.sh`가 필요하다.

해피에게 "그록으로" 요청하면 `--engine grok`으로 배정한다. 기본은 Claude다. `!모델 그록1 <모델> [effort]`는 `grok models` 목록의 모델만 허용하고 그록 공통 기본값만 바꾼다.

로그인 만료: 스레드에 `⛔ Grok 로그인이 만료됐습니다` 안내가 올라오고 `status.sh`가 ⛔로 표시한다. 관리 터미널에서 `grok login` 후 같은 메시지를 스레드에 다시 보내면 된다. 브리지는 재기동하지 않아도 된다.
샌드박스는 기본으로 끈다(마크와 같은 수준). `routes.json`의 `grokSandbox`에 프로필 이름을 넣으면 `--sandbox <이름>`을 붙인다. 프로필은 `~/.grok/sandbox.toml`에 직접 추가한다 (`doctor`가 필요한 항목을 안내한다). 오케스트레이터 자체 작업(운영 적용)은 샌드박스에서 동작하지 않는다.

### 기존 설정 작업

- 새 프로젝트: Git 기준 커밋 확인 → Orca 등록 → routes 추가 → 접수원과 자비스 재시작. [상담역 온보딩](../roles/상담역-온보딩.md) 참고.
- 모델·역할·env 변경: 상시 Claude는 재기동, 마크는 다음 스폰, 자비스는 restart. 자비스의 기존 Codex resume 세션에는 이전 지침이 남을 수 있다.
- 비전 설정 변경: `stop` 후 `fresh` 또는 `resume`.
- 역할 비활성: 실행 중인 해당 세션을 먼저 종료하고 enabled를 변경한다. 자비스는 `jarvis-down.sh`, 비전은 `vision.sh stop`, 마크는 각 스레드 종료, 상시 Claude는 `<STATE_DIR_ROOT>/leads/<역할>.term`의 핸들을 확인해 Orca에서 해당 터미널을 닫는다. 자동 실행을 이미 설치했다면 해당 launchd job을 먼저 해제한다.
- 설정 경로 변경: 새 셸/세션에 경로를 적용하고 launchd 파일을 다시 생성·등록한다.

플러그인은 `ensure_plugin`이 세션 cwd에 project 스코프로 설치한다. `--plugin-dir`를 추가하지 않는다. 기동 시 플러그인 자식과 터미널 배너를 확인하지만, Discord 메시지 왕복 테스트가 최종 연결 확인이다.

일반 셸의 `claude auth status`가 정상인데 봇에 `Login expired` 또는 `Not logged in`이 뜨면 **Orca 안의 터미널에서도** 같은 명령을 확인한다. macOS 실행 환경에 따라 인증 저장소 접근 결과가 다를 수 있다. Orca 쪽이 로그아웃 상태라면 그 터미널에서 `claude auth login --claudeai`로 인증한 뒤 해당 봇을 재시작한다. 토큰을 터미널 명령 인자에 복사하지 않는다.

자비스의 정상 종료는 exit 0이므로 **jarvis-down만으로 자동 재시작된다고 가정하지 않는다.** 코드·라우트 변경에는 `jarvis-restart.sh`를 쓴다. 인증 갱신은 `codexAuthFile`의 원본 계정을 다시 로그인하면 끝이다(자비스 `auth.json`은 원본으로의 링크). `reauth`는 강제 재링크만 한다 — 예: #운영-로그에 "원본보다 새것이라 링크하지 않고 유지 중" 경고가 뜬 뒤 원본 로그인 상태를 확인했을 때:

```sh
# lib.sh의 경로 환경을 동일하게 적용하기 위해 zsh에서 실행한다.
zsh -c 'source ./bin/lib.sh; bun "$ORCH_ROOT/jarvis/server.ts" reauth'
```

## 소유자 DM

### 프라이데이 모델 관리 (Claude 호출 없음)

프라이데이 1:1 DM에 명령을 직접 입력한다. 일반 채팅이나 Discord 슬래시 명령이 아니라 `!`로 시작하는 텍스트 명령이다.
Discord 플러그인이 수신 즉시 처리하므로 Claude API 사용량 제한 중에도 동작한다. 플러그인/PC 자체가 꺼져 있으면 동작하지 않는다.

| 명령 | 동작 |
|---|---|
| `!모델` | 설정 기본값과 최근 변경 결과 (실제 응답 성공 여부와 다름) |
| `!모델 목록` | Claude 별칭 및 로컬 Codex 카탈로그의 모델/effort |
| `!모델 해피 sonnet medium` | 변경 영향과 확인 코드 표시, 아직 저장하지 않음 |
| `!모델 자비스 gpt-6-astra xhigh` | 자비스 변경 제안 |
| `!모델 확인 <코드>` | 5분 내 동일 소유자·동일 DM에서 확정하고 적용 |
| `!모델 취소` | 미확정 제안 취소 |

effort 생략 시 기존 설정 유지, `default`이면 CLI 기본값. 자비스의 `default default`는 모델과 effort 모두 기본값으로 복원한다.
프라이데이·해피는 해당 세션을 재시작하므로 진행 중 응답/컨텍스트가 끊길 수 있다. 자비스도 재시작하고 다음 요청을 새 Codex 대화로 시작하되 이전 기록은 보존한다.
마크는 작업자 공통 기본값만 변경하며 실행 중·이미 대기열에 들어간 작업·이전 세션 재개 설정은 바꾸지 않는다. 그록 봇(`!모델 그록1 …` 또는 `!모델 그록 …`)은 `grok작업자` 공통 기본값을 같은 방식으로 바꾼다.
Claude 봇은 Claude 모델만, 그록 봇은 `grok models` 목록의 모델만, 자비스는 Codex 모델만 선택할 수 있다. 모델 변경은 계정 한도를 해제하지 않는다.

구현: `plugin/discord-orca/model-control.ts` → `bin/model_control.py` → 확인 후 분리 프로세스에서 `bin/model-restart.sh`.
모델/effort 이외의 설정은 보존하고 직전 routes를 `state/model-control/routes-before.json`에 백업한다.
재시작 실패 시 설정 저장과 적용 실패를 구분해 알리며 `!모델`에서도 결과를 볼 수 있다. 주기 감시나 모델 호출은 추가하지 않는다.
플러그인 코드 배포 후 실행 중인 프라이데이의 `/mcp`에서 `plugin:discord-orca:discord`를 Reconnect해야 한다.

### 접수 난이도 라우팅 (Jev)

`spawn-worker.sh`는 `--model`/`--effort`가 없고 claude 엔진의 새 작업이면 `bin/classify-request.py`로 요청 본문과 프로젝트 이름만 Jev(`typesafe/jev-1.13-20260917`, OpenRouter)에 보내 `simple|standard|hard`를 받는다. 매핑은 `routes.json`의 `jevRouting`(없으면 내장 기본값: simple→sonnet/medium, standard→`models.작업자`, hard→fable/high, `threshold` 0.85, `timeoutSec` 3, `enabled`)이다. 우선순위는 사용자 명시(해피의 `--model`/`--effort`) > 봇 항목 model/effort > Jev > 엔진 기본값이며 Grok 엔진·재개는 분류하지 않는다. 키(`OPENROUTER_API_KEY` 환경변수 > 공통 OpenRouter 키 파일, 아래) 없음·API 실패·타임아웃·확신도 미달은 기본값으로 스폰한다. 등록부에 `routeLevel`·`routeConfidence`·`routeSource`(user|jev|default)를 남기고 대기열 재시도는 이 판정을 그대로 쓴다. 배정 표시는 `(opus/medium · 자동|지정|기본)`. 확인은 `spawn-worker.sh <채널> <스레드> --request-file <f> --dry-run`의 `model=… source=…` 줄과 stderr `난이도 분류:` 줄(`reason=`)로 한다.

### OpenRouter 키 (공통)

OpenRouter를 쓰는 기능(Jev 라우팅·툴 게이트, 이후 추가분도)은 `config.openrouter_key_file(s)`로 키 파일을 찾는다. 순서는 `routes.json` 최상위 `openrouterKeyFile`(예: `~/.hermes/.env`처럼 다른 에이전트가 쓰는 env 파일 경로) → `$BOTS_DIR/openrouter.env`. 키 값을 복사하거나 출력하지 않고 경로만 둔다. 원본 파일의 키가 바뀌면 여기도 함께 바뀌고 사용량은 같은 계정에 합산된다.

### 마크 서브에이전트 모델 티어

마크가 Agent 툴로 위임할 때 작업 종류별로 모델을 고른다. 탐색·grep·파일 훑기·웹검색·문서 읽기는 sonnet, 정형 반복(고정 패턴 변환·대량 단순 편집·포맷 맞추기)은 haiku, 일반 구현·조사 종합·리뷰는 opus(마크 기본), 복잡한 설계·난이도 높은 디버깅·사용자가 "fable로" 라고 한 작업은 fable.
규칙은 `roles/작업자.md`(`--append-system-prompt-file` 주입)에만 두고 전역 `~/.claude`에는 넣지 않는다. 마크가 Agent 호출의 `model` 인자로 모델을 고른다. `CLAUDE_CODE_SUBAGENT_MODEL`은 기본 모델 하나만 정하고 `_FORCE`는 호출별 선택을 무시하므로 쓰지 않는다. `model`을 빠뜨리면 부모 모델(opus)을 이어받는다.
확인은 세션 기록 `~/.claude/projects/<경로>/<세션>/subagents/agent-*.jsonl`의 `"model"` 필드로 한다.

### 해피·자비스 DM

해피와 자비스는 `routes.json`의 `ownerUserId`에 지정된 본인의 1:1 DM만 받는다.
DM에서는 멘션이 필요 없고, 답변도 같은 DM으로 보낸다. 다른 사용자의 DM·그룹 DM·봇 DM은 허용하지 않는다.

- 해피: 인사·접수 사용법·상태 확인에 응답한다. 실제 작업 배정은 프로젝트 채널/작업 스레드에서 한다.
  DM 내용을 자동으로 서버에 옮기거나 DM ID로 작업자를 띄우지 않는다.
- 자비스: 질문·검토 요청에 응답한다. DM별로 독립된 대화 상태를 사용하며, 대상이 불분명하면 프로젝트를 묻는다.
  읽기 전용 정책은 유지한다. 서버 채널에서는 기존처럼 직접 `@자비스` 멘션이 필요하다.
- 해피 접근 설정은 `bin/lead-up.sh`가 생성한다. static 모드라 실행 중인 Discord 플러그인을 재연결하거나
  접수원을 재시작해야 수정된 허용 목록이 적용된다. 자비스 코드 변경은 `bin/jarvis-restart.sh`로 적용한다.
- 변경 전에 보낸 DM은 자동 재처리하지 않는다. 적용 후 새 메시지부터 수신한다.

## 모델 오류 알림 (이벤트 기반)

Claude 봇(프라이데이·해피·마크)은 `templates/progress-settings.json`의 `StopFailure` 훅으로
사용량 제한·인증 실패·서버 오류를 감지한다. `bin/progress-hook.py` → `bin/failure_hook.py`가
마지막 대화 채널/작업 스레드와 운영 로그에 원인을 전송한다. 모델 호출이나 주기 감시는 없다.
전역 Orca 훅은 변경하지 않는다. 자비스는 Codex 기반이므로 이 Claude 훅의 대상이 아니다.

- 사용량 제한 문구에 재개 시간이 있으면 그대로 표시하고, 없으면 `제공되지 않음`으로 알린다.
- 같은 오류·재개 시간은 채널별 한 번만 알린다. 새 채널에서 다시 실패하거나 시간이 바뀌면 다시 알린다.
- `state/leads/<역할>.failure.json` 또는 `state/threads/<스레드>.failure.json`에 실패 상태를 저장한다.
  `bin/status.sh`는 같은 PID의 실패 기록이 있으면 ✅ 대신 `⛔ 응답 불가`를 표시한다.
- 예상 재개 시간이 지나기만 했다고 정상으로 바꾸지 않는다. 실제 `Stop`(정상 턴 종료)이 확인되면 해제하고 복구 알림을 보낸다.
- Discord 전송에 실패해도 실패 상태는 남는다. 다음 오류 이벤트에서 미전송 채널에 다시 시도하며 별도 재시도 데몬은 없다.
- 훅 오류가 발생하면 기존 진행 표시와 typing 유지도 종료한다. 프로세스 강제 종료·Discord 연결 단절처럼
  `StopFailure`가 발생하지 않는 장애까지 감지하는 기능은 아니다.

새 세션은 템플릿을 읽는다. 실행 중인 세션의 `--settings` 파일은 즉시 다시 읽히지 않을 수 있으므로
`/hooks`에서 `StopFailure`에 `progress-hook.py`가 등록되었는지 확인한다.
대화를 유지하며 적용할 때는 해당 세션 cwd의 `.claude/settings.local.json`에 같은 훅을 병합할 수 있다.
기존 설정을 덮어쓰지 않는다.

## 툴 게이트 (Jev, 마크 Bash)

`templates/progress-settings.json`의 PreToolUse(matcher `Bash`)로 `bin/tool-gate.py`가 마크의 Bash 호출마다 돈다
(`ORCA_THREAD_ID` 없는 상시 세션·그록·자비스는 대상 아님). 규칙으로 바로 allow 하는 것: 읽기 전용 셸·git 조회, `git add`/`commit`
(작업 폴더 안), `<ORCH_ROOT>/bin/post-result.sh`·`finish-worker.sh`(절대 경로·`$ORCH_ROOT`·앞서 대입한 변수·`cd <ORCH_ROOT> &&` 뒤 상대 경로),
작업 폴더(`CLAUDE_PROJECT_DIR`, 없으면 훅 cwd)·세션 스크래치(`/private/tmp/claude-<uid>/<슬러그>/`, `/tmp/…`)로의 `>`·`>>`·`mkdir -p`,
`<ORCH_ROOT>/runs/**/*.md` 덧붙이기(`>>`만), `until`/`while … ; do …; done` 대기 루프(안의 명령을 각각 검사, `sleep`·`pgrep` 허용). 따옴표를 따라가는 스캐너가 명령·프로세스 치환(작은따옴표 안, `` \` ``·`` \$( `` 이스케이프는 제외),
따옴표 없는 heredoc 본문의 치환, 서브셸을 거르고(단 `$(date +형식)`은 허용),  같은 명령 앞쪽의 `NAME=값` 대입과 `cd`를 따라 경로를 펼친다(`..` 정규화,
모르는 변수·glob·`~user`면 Jev). `cd` 뒤 cwd는 같은 `&&` 사슬 안에서만 새 경로로 보고, `;`·`||`·`|`·줄바꿈 뒤에는 cd 실패 때의 cwd도 함께 본다. `PATH`·`GIT_*`·`*PAGER` 같은 대입은 규칙 allow 하지 않는다.
나머지는 OpenRouter Decisions API의 Jev(`toolGate.model`)에 State/Choice(allow·ask·deny)로 묻는다. State에 작업 폴더·스크래치 경로와
"runs 로그 덧붙이기·post-result/finish-worker 호출은 필수 보고 절차"를 넣는다.
최종 판정은 코드가 한다: allow는 확신도 `allowThreshold`(기본 0.5), deny는 `threshold`(기본 0.85) 미만이면 ask. 키는 공통 OpenRouter 키 파일의
`OPENROUTER_API_KEY=` 한 줄이며, 키 없음·3.5초 타임아웃·네트워크·응답 형식 오류는 allow(fail-open)하고 기록에 `error`를 남긴다.
기록은 `state/tool-gate/<스레드>.jsonl`(ts·command 앞 300자·decision·verdict·confidence·ms·mode·source·error·qv).
`qv`는 질문·규칙 버전(`QUESTION_VERSION`)이라 바꾸기 전후 기록을 가른다. `source`: rule·jev·fail-open·approval.
`routes.json` `toolGate.mode`: `shadow`(기본, 판정·기록만 하고 훅은 즉시 반환 — 분리 자식이 처리), `enforce`, `off`.
섀도 누적 집계는 `state/tool-gate/summary.json`이며, Jev 판정 수·마크 작업 수가 `toolGate.reviewAt`(기본 50건·10개)에
처음 닿으면 운영 로그 채널에 소유자 멘션으로 검토 알림(판정 분포·확신도 미달·fail-open 수)을 한 번 올린다. 다시 받으려면 `summary.json`을 지운다.
`summary.json`에 `"reviewChannel": "<스레드·채널 ID>"`를 넣으면 그쪽으로 보낸다(보관된 스레드도 글을 올리면 다시 열린다). 실패하면 운영 로그 채널.
`toolGate.notify: true`면 섀도에서 ask/deny 판정을 스레드에 한 줄 알린다(기본 off).
**재측정**: 규칙·질문을 바꾼 뒤에는 `summary.json`을 보관(`summary-<날짜>.json`)하고 새로 집계한다(`qv`로 새 기록만 본다).
재알림 후 부당 ask 비율이 10% 아래면 enforce.
**enforce 전환**: 섀도 기록에서 오판(`source=jev`인데 부당한 ask/deny)을 먼저 확인한 뒤 `toolGate.mode`를 `"enforce"`로 바꾼다.
훅이 매번 설정을 읽으므로 재시작은 필요 없다. enforce는 동기 판정이며 deny는 차단(승인 불가), ask는 차단 + 확인 요청이다
(Claude의 `ask` 결정은 터미널 확인을 띄워 마크가 멈추므로 쓰지 않는다).
**승인 흐름 (enforce)**: Jev ask면 게이트가 마크 봇으로 스레드에 `` 🛡 확인 필요 (@소유자): `명령` ``을 올리고
`state/tool-gate/<스레드>.approvals.json`에 명령 전체의 sha256 키로 `{messageId, command, requested, approved}`를 남긴 뒤,
"기다렸다가 승인되면 같은 명령을 그대로 다시 실행하라"는 사유로 막는다. 소유자(`ownerUserId`)가 그 메시지에 ✅를 누르고 스레드에
"진행"이라고 쓰면, 마크의 재시도 때 게이트가 반응을 조회해 승인으로 기록하고 allow한다. 승인은 60분 유효(지나면 다시 요청).
대기 중 재시도는 Jev 없이 막고 메시지를 다시 올리지 않는다. 승인 조회는 규칙 다음·Jev 전, Discord 호출은 1.5초 이내.
게시 실패(토큰 없음 등)면 요청 없이 막기만 한다. 섀도에서는 승인 메시지를 올리지 않는다.

## 자동 실행 등록

수동 사용 확인 후 실행한다. 아래 작업은 봇을 실제로 기동한다. 생성 단계만으로는 등록·기동되지 않는다.

```sh
python3 bin/setup.py launchd
```

생성 경로 기본값은 `state/launchd/`. 템플릿의 실제 설치 경로, PATH, 인증 원본 경로와 활성 역할을 반영한다. `manifest.json`에 이번 구성에 필요한 파일만 담긴다. 예전 생성 파일이 남아도 디렉터리 전체 glob을 등록하지 않는다.

등록 예시 (기존 동일 이름 LaunchAgent가 없는 최초 설치):

```sh
python3 - <<'PY'
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path('bin').resolve()))
from config import runtime_paths
folder = Path(runtime_paths()['STATE_DIR_ROOT']) / 'launchd'
agents = Path.home() / 'Library/LaunchAgents'
agents.mkdir(parents=True, exist_ok=True)
files = [Path(p) for p in json.loads((folder / 'manifest.json').read_text())]
for p in files:
    dest = agents / p.name
    if dest.exists() or dest.is_symlink():
        raise SystemExit(f'기존 등록 확인 필요: {dest}')
for p in files:
    dest = agents / p.name
    dest.symlink_to(p)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(dest)], check=True)
PY
```

| job | 동작 |
|---|---|
| `ai.orca.leads-check` | 5분마다 활성 상담역·접수원 복구 및 켜 둔 비전 재개 |
| `ai.orca.접수원-restart` | 매일 04:00 접수원 새 세션 |
| `ai.orca.sweep` | 5분마다 작업 정리 |
| `ai.orca.jarvis` | 로그인 시 기동, 비정상 종료 시 재기동 |

```sh
launchctl print "gui/$(id -u)/ai.orca.jarvis"
launchctl kickstart -k "gui/$(id -u)/ai.orca.jarvis"
# 해제 / 파일 수정 후 재등록할 때 (대상 label을 정확히 지정)
launchctl bootout "gui/$(id -u)/ai.orca.jarvis"
```

역할을 영구적으로 끄면 해당 LaunchAgents symlink도 제거해야 다음 로그인 때 기동되지 않는다. 이미 켜진 Claude 봇은 감시 job 해제만으로 종료되지 않는다. 로그는 `<STATE_DIR_ROOT>/log/`에 있다. 재부팅 후 마크의 이전 Claude 세션을 자동 복원하지는 않는다.

## sweep 자식 프로세스 유지

`launchd/ai.orca.sweep.plist`에는 `AbandonProcessGroup=true`를 유지한다. sweep가 종료된 뒤에도 대기열 재시도용 자식 프로세스가 launchd의 프로세스 그룹 정리로 종료되지 않도록 하는 설정이다.

변경 시 `python3 bin/setup.py launchd`로 생성하고, sweep가 실행 중이 아닌지 확인한 다음 `ai.orca.sweep`만 bootout/bootstrap한다. 생성기는 현재 셸의 PATH와 실행 파일 경로를 반영하므로 기존 등록 환경을 보존하고 다른 job의 생성 파일이 달라지지 않았는지 확인한다.

```sh
launchctl bootout "gui/$(id -u)/ai.orca.sweep"
launchctl bootstrap "gui/$(id -u)" "$PWD/state/launchd/ai.orca.sweep.plist"
launchctl print "gui/$(id -u)/ai.orca.sweep"
```

검증: 템플릿과 생성 plist의 boolean 값이 true이고 `plutil -lint`가 통과해야 한다. 실제 launchctl 출력의 `properties = abandon process group | inferred program`도 적용 증거다 (`abandon process group = 1` 형식만 기대하지 않는다). 이번 적용에서는 이 런타임 속성과 300초 주기, 다른 생성 파일 불변을 확인했다. 실제 새 요청의 queued → 자동 시작 시나리오는 별도 검증 대상이며 설정 적용만으로 성공했다고 간주하지 않는다.

## 테스트

[README 검증 명령](../README.md#구현과-검증)으로 정적·로컬 테스트를 실행한다. 실제 적용 확인은 [최초 사용 확인](SETUP.ko.md#최초-사용-확인)을 따른다. 테스트의 모의 Orca가 실제 앱·Discord·계정 호환성까지 보장하지는 않는다.
