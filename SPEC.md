# 오케스트레이터 설계

4봇 기본 구성: 프라이데이(기획), 해피(접수), 마크1(작업), 자비스(검수). 비전은 선택 사항이다. 봇 하나는 연결 프로세스 하나에 대응한다. 프라이데이·해피·마크는 Claude Code, 자비스는 Codex exec, 비전은 Codex app-server로 고정되어 있다. 모델 이름만으로 엔진이 바뀌지 않는다.

## 수신과 컨텍스트

| 역할 | 허용 입력 | 컨텍스트 |
|---|---|---|
| 상담역 | 기획 채널·하위 스레드, 소유자 DM | 상시 Claude 세션. 재기동은 새 세션 |
| 접수원 | 프로젝트 최상위, active 작업자가 없는 스레드, 운영 채널 | 상시 Claude 세션. 자동 운영 시 매일 04:00 초기화 |
| 작업자 | 배정 스레드의 소유자 입력 | 스레드별 Claude 세션 + 별도 Git worktree |
| 리뷰어 | 허용 채널에서 소유자 또는 활성 상담역·작업자의 리뷰어 멘션 | Discord chat별 Codex 세션 |
| 비전 | 전용 라운지와 하위 스레드의 소유자 입력 | 라운지 전체가 단일 Codex 세션 |

Claude 플러그인은 `access.json`의 소유자 allowlist와 채널 목록에 따라 입력을 제한한다. 상담역만 DM을 허용한다. `DISCORD_ONLY_CHATS`는 작업자의 스레드를, `DISCORD_TOP_LEVEL_ONLY`와 등록부는 접수원의 수신 범위를 제한한다. 다른 봇만 멘션한 메시지는 일반 봇이 받지 않는다.

봇·웹훅 입력은 Claude 플러그인과 비전에서 차단된다. 자비스는 활성 상담역·작업자의 Application ID만 신뢰하며, 봇별·chat별 60초에 3회 제한이 있다. 신뢰 봇의 검수 브리프는 사용자가 요청했을 때만 보내도록 역할 지침이 정한다. 검수 결과의 자동 역전달은 없다.

## 작업 생성과 종료

1. 해피 또는 상담역이 요청 파일과 제목 파일을 만들고 `open-thread.sh`로 스레드를 연다.
2. `spawn-worker.sh`가 스레드 잠금 아래에서 봇을 대여한다. 풀이 차 있으면 `queuedAt`과 요청을 기록하고 exit 3으로 끝난다.
3. 새 작업마다 `task-<threadId>-<sessionId 일부>` 이름의 Orca child worktree를 생성한다. 기준은 원본 저장소의 현재 HEAD 커밋이다. 원본 미커밋 변경을 자동으로 가져오지 않는다.
4. Git 루트·공통 Git 저장소·원본과 다른 경로를 검증한다. 실패하면 작업자를 띄우지 않고 봇을 반환한다. 터미널 생성 실패 때도 실패 등록과 반환을 수행하며 생성된 worktree는 보존한다.
5. project 스코프 플러그인 설치 후 새 Orca 터미널에 경로·수신 범위·역할 프롬프트를 전달한다. 초기 플러그인 연결 확인에 실패하면 한 번 재시도하고 경고한다.
6. 결과 보고 후에도 세션을 유지한다. 종료 지시 또는 sweep의 30분 유휴 판정으로 스레드 보관·터미널 종료·봇 반환과 대기열 재시도를 수행한다.

대기열은 `queuedAt` 우선으로 정렬한다. sweep은 스폰 잠금이 잡힌 lease를 고아로 회수하지 않는다. 결과 worktree·브랜치는 자동 병합·삭제하지 않는다. 검토 후 사용자가 병합 및 정리 정책을 선택한다.

같은 스레드의 `--resume`은 저장된 Claude sessionId와 같은 격리 worktree를 사용한다. 기본 재요청은 새 worktree다. 과거 공유 폴더 세션의 resume은 거부한다. 다른 세션으로 인계할 때는 사용자가 요청한 인계 문서 경로만 전달한다.

## 경로와 역할

`bin/config.py`가 루트·설정·상태·봇 env·검수 인증 경로를 정하고 `bin/lib.sh`가 내보낸다. Orca PTY가 호출 셸 환경을 그대로 상속한다고 가정하지 않는다. 진행 훅과 생성된 launchd 파일도 같은 경로 계약을 사용한다.

`enabled` 값이 있으면 우선한다. 이전 설정에 이 값이 없으면 봇 항목과 lounge 존재 여부로 추론한다. 비활성 역할은 자동 기동·상태 복원 대상에서 제외한다. 끄기 전에 기존 프로세스를 종료해야 하며 설정 변경만으로 실행 중인 봇이 종료되지는 않는다.

`setup.py doctor`는 파일·도구·로그인·Orca 등록을 검사한다. Discord 접속과 실제 모델 실행은 검사하지 않는다. `init`과 `bot-env`는 기존 파일을 덮어쓰지 않는다. 두 dry-run과 pool status는 런타임 상태를 변경하지 않는다.

## 검수와 독립 Codex

자비스는 스레드 이력, 등록부의 worktree path/baseRef, git 상태를 Codex에 전달한다. 기본 검수 동시 실행 수는 1이다. 새 채널/신뢰 봇 설정은 데몬 재시작이 필요하다. Codex 홈은 `<STATE_DIR_ROOT>/jarvis/codex-home`이며 원본 인증 파일을 최초 한 번, 또는 명시적 reauth 때 복사한다. 개인 Codex 설정 전체를 복사하지 않고 읽기 전용·approval never 설정을 만든다.

비전은 `fresh`, `resume`, `attach`, `stop`, `status`로 제어한다. 브리지가 app-server를 소유하므로 TUI만 닫아도 백엔드는 유지된다. 중복 억제 저널은 at-most-once 방식을 사용하며 무손실 전달을 보장하지 않는다. 기본 구성에서는 lounge와 비전 토큰이 필요 없다.

## 파일 배치

| 위치 | 내용 |
|---|---|
| `routes.json` | 개인 라우트·역할·모델·인증 원본 경로, git 제외 |
| `bin/` | 설정·진단·스폰·종료·감시·진행 표시 |
| `plugin/discord-orca/` | 로컬 marketplace의 Discord 플러그인 |
| `jarvis/`, `codex-worker/` | 검수 데몬, 선택형 비전 브리지 |
| `roles/`, `sessions/` | 역할 지침, 상시 Claude 세션 cwd |
| `state/threads/`, `state/pool.json` | 작업 등록부, 봇 대여 상태 |
| `state/leads/`, `state/jarvis/`, `state/vision.codex/` | 역할별 프로세스·세션 상태 |
| `state/launchd/`, `state/log/` | 생성된 자동 실행 파일, 운영 로그 |
| `runs/<project>/` | 작업·검수 결과 로그 |
| `launchd/` | 경로 치환 전 템플릿 |

봇 원본 env는 `<stateRoot>/bots/`, Claude 연결용 복사본은 `<stateRoot>/roles/<역할>/` 또는 `<stateRoot>/workers/<threadId>/`에 있다. stateRoot 기본값은 `~/.claude/channels`이며 BOTS_DIR 환경변수로 원본 위치를 별도 지정할 수 있다.
