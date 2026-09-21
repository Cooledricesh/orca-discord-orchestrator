# orca-discord-orchestrator

개인 Mac의 Claude Code·Codex 작업을 Discord에서 지시하고 확인하는 도구다. Orca가 터미널과 작업별 Git worktree를 관리한다. 이 버전의 기본 구성은 **4봇 · 프로젝트 1개 · 동시 작업자 1개**다.

| 봇 | 하는 일 | 실행 방식 |
|---|---|---|
| 프라이데이 / 상담역 | 기획 채널과 소유자 DM에서 논의, 확정된 작업 전달 | Claude Code, 기본 `opus` |
| 해피 / 접수원 | 프로젝트 채널의 요청을 스레드와 작업자에 배정 | Claude Code, 기본 `sonnet` |
| 마크1 / 작업자 | 자기 스레드에서 개발·조사·후속 수정 | Claude Code, 기본 `opus`, 작업 전용 worktree |
| 자비스 / 리뷰어 | 멘션으로 요청받은 내용을 읽기 전용 검수 | Codex `exec`, chat별 세션 |
| 비전 / 선택 사항 | 전용 라운지에서 독립적인 Codex 작업 | Codex `app-server`, 기본 비활성 |

해피의 접수에도 모델 호출이 발생한다. 마크 수는 `bots.workers` 길이로 정한다. 비전은 활성화해도 라운지와 그 하위 스레드가 **하나의 Codex 컨텍스트**를 공유한다.

## 사용 흐름

1. 프로젝트 채널에 `로그인 화면에 비밀번호 표시 버튼을 추가해줘`라고 쓴다.
2. 해피가 스레드를 만들고 마크를 배정한다. 새 작업은 프로젝트의 현재 **커밋 HEAD**를 기준으로 별도 worktree에서 시작한다. 원본 폴더의 미커밋 변경은 포함되지 않는다.
3. 같은 스레드에 후속 지시를 쓴다. 결과가 나와도 마크는 대기한다. 풀이 사용 중이면 다른 요청은 대기열에 들어간다.
4. `@자비스 이번 변경을 검수해줘`라고 요청한다. 사용자 요청에 따라 프라이데이·마크도 검수 브리프를 보낼 수 있다. 자비스 결과는 사람이 마크에게 전달한다.
5. `종료` 또는 `중단`으로 봇을 반환한다. sweep이 실행 중이면 30분 이상 유휴 작업도 정리한다. **worktree의 병합과 삭제는 자동으로 하지 않는다.**

종료된 스레드의 새 요청은 새 worktree·새 세션으로 시작한다. 직전 작업을 유지하려면 `이전 세션 이어서`를 명시한다. 이때 같은 격리 worktree에서 Claude 세션을 복원한다. 예전 버전이 원본 폴더에서 실행한 세션은 재개를 거부한다.

오케스트레이터 자체를 프로젝트로 등록하면 해당 채널의 장애 수정 요청은 수정·검증·커밋 후 **운영본 적용과 대상 서비스 재시작까지** 처리한다. 프라이데이는 요청을 이 프로젝트로 넘기거나 정해진 운영 작업을 직접 실행한다. 별도 관리 터미널로 명령을 옮길 필요가 없다. 운영본에 미커밋 변경이 있으면 새 작업을 거부해 오래된 코드에서 작업하는 것을 방지한다. [운영 적용 절차](roles/운영-적용.md).

## 설치

macOS, 실행 중인 Orca, Claude Code, Codex, Bun, Python 3, Git이 필요하다. Discord에는 소유자 한 명과 4개의 봇 애플리케이션을 준비한다. 최신 인증·권한 요건과 실제 입력 순서는 [적용 가이드](docs/SETUP.ko.md)에 있다.

어느 폴더에 클론해도 스크립트가 자신의 위치를 기준으로 루트를 찾는다.

```sh
git clone https://github.com/pcepyon/orca-discord-orchestrator.git
cd orca-discord-orchestrator
for d in plugin/discord-orca jarvis codex-worker; do
  (cd "$d" && bun install --ignore-scripts)
done
python3 bin/setup.py init
```

`routes.json`의 프로젝트 절대 경로와 Discord ID를 채운다. `init`은 기존 파일을 덮어쓰지 않는다. 봇별 토큰과 Application ID는 직접 연 터미널에서 숨김 입력으로 저장한다.

```sh
python3 bin/setup.py bot-env 프라이데이
python3 bin/setup.py bot-env 해피
python3 bin/setup.py bot-env 마크1
python3 bin/setup.py bot-env 자비스
python3 bin/setup.py invites
claude plugin marketplace add "$PWD/plugin"
orca repo add --path "$PWD" --json
# 실제 프로젝트도 Orca에 등록한다.
orca repo add --path /absolute/path/to/project --json
python3 bin/setup.py doctor
```

`doctor`는 로컬 준비 상태만 확인한다. 토큰의 실제 유효성, Discord 채널 권한, Claude 개발 채널 연결, 모델 이용 가능성은 최초 기동으로 확인해야 한다. 준비가 끝나면 수동 기동한다.

```sh
bin/lead-up.sh 상담역
bin/lead-up.sh 접수원
bin/jarvis-up.sh
bin/status.sh
```

마크는 Discord 요청이 오면 자동으로 배정된다. 수동 사용 흐름 확인 후 `python3 bin/setup.py launchd`로 **현재 경로가 들어간** 자동 실행 파일을 생성한다. 등록 방법은 [운영 가이드](docs/ADMIN.md)를 따른다. `launchd/`의 원본 템플릿을 직접 등록하지 않는다.

## 설정과 저장 위치

| 항목 | 기본값 / 의미 |
|---|---|
| `enabled` | 상담역·접수원·작업자·리뷰어 `true`, 비전 `false` |
| `ORCH_ROOT` | 설치 폴더 자동 탐지. 필요할 때 환경변수로 재정의 |
| `ROUTES_FILE` | `<ORCH_ROOT>/routes.json` |
| `STATE_DIR_ROOT` | `<ORCH_ROOT>/state`: 등록부·풀·로그·검수 세션 |
| `routes.json stateRoot` | `~/.claude/channels`: Discord 세션별 설정·토큰 복사본 |
| `BOTS_DIR` | `<stateRoot>/bots`: 원본 봇 env 파일 |
| `codexAuthFile` | 리뷰어에 복사할 인증 파일 경로. `init`이 현재 `CODEX_HOME/auth.json` 경로를 기록 |
| `ORCH_CODEX_AUTH_FILE` | `codexAuthFile`보다 우선하는 환경변수 |
| `models.리뷰어` | 빈 model/effort면 격리 Codex CLI의 기본값. 개인 `config.toml` 전체를 복사하지 않음 |
| `writeDir` | worktree 내부 상대 경로. 역할 지침상의 수정 범위 |

실행 스크립트가 새 Orca 터미널에도 위 경로를 전달한다. 역할 문서의 `$ORCH_ROOT` 등은 시작 프롬프트에 적힌 실제 경로로 읽는다. 봇 이름을 바꾸면 `bots`, 표시 이름, 해당 env 파일을 함께 맞춘다. 역할을 끌 때는 실행 중인 프로세스를 먼저 종료하고 `enabled`를 바꾼다.

## 구현과 검증

[설계](SPEC.md) · [적용 준비](docs/SETUP.ko.md) · [운영 명령](docs/ADMIN.md)

```sh
python3 -m unittest discover -s tests -v
(cd codex-worker && bun run typecheck && bun test)
(cd jarvis && bun run typecheck && bun test)
(cd plugin/discord-orca && bun test)
for f in bin/*.sh lead.sh; do zsh -n "$f" || exit 1; done
```

`bin/spawn-worker.sh <채널ID> <스레드ID> --dry-run`과 `DRY=1 bin/lead-up.sh 접수원`은 풀·등록부·봇 env·플러그인 설치·Orca를 변경하지 않는다. 테스트는 임시 저장소와 모의 CLI를 사용하며 실제 Discord나 모델을 호출하지 않는다.

## 접근 범위

소유자 한 명의 개인 운영을 전제로 한다. Claude 봇은 `--dangerously-skip-permissions`, 비전은 `danger-full-access`로 실행된다. **별도 worktree와 `writeDir`은 OS 접근 권한을 제한하는 샌드박스가 아니다.** 자비스는 Codex 읽기 전용 검수로 실행된다.

Claude 플러그인과 비전은 봇·웹훅 메시지를 차단한다. 자비스만 설정된 프라이데이·마크의 멘션을 허용한다. 자동 검수·수정 반복 루프는 없다.

`routes.json`, `state/`, `runs/`와 봇 env는 개인 자료다. 토큰은 원본 env와 세션별 복사본에 존재하며 출력·커밋하지 않는다. 인증 파일과 상태 폴더의 Discord 첨부를 플러그인이 차단한다.

## 라이선스

Apache-2.0. `plugin/discord-orca/`는 Anthropic의 Claude Code Discord 채널 플러그인을 수정한 것이다. [NOTICE](NOTICE) 참조.
