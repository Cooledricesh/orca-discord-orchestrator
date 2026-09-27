# 개인 적용 가이드

2026-09-12 기준. 처음에는 프라이데이·해피·마크1·자비스, 프로젝트 한 개로 시작한다. Discord 봇 4개가 서로 다른 연결을 담당하며 Claude/Codex 계정을 봇마다 따로 만들 필요는 없다. 모델 이용 한도는 인증한 계정에 따른다.

## 준비할 값

| 항목 | 넣을 곳 | 예시 / 조건 |
|---|---|---|
| 작업할 프로젝트 이름·절대 경로 | `routes[프로젝트채널ID].name`, `.path` | `my-app`, `/Users/me/Project/my-app` |
| Discord 서버 ID | `guildId` | 숫자 문자열 |
| 본인 사용자 ID | `ownerUserId` | 지시를 허용할 소유자 한 명 |
| 기획 채널 ID | `generalChannelId` | 예: `#기획` |
| 운영 로그 채널 ID | `opsLogChannelId` | 예: `#운영-로그` |
| 프로젝트 채널 ID | `routes`의 키 | 예: `#my-app` |
| 봇별 Application ID + Bot Token | `bot-env`로 저장 | 프라이데이·해피·마크1·자비스 각각 하나 |

Discord ID 5개와 프로젝트 경로는 설정 파일에 넣는다. 토큰 4개는 직접 연 터미널에서 입력한다. 토큰을 이 문서나 채팅에 적지 않는다. 프로젝트는 유효한 HEAD 커밋이 있는 Git 저장소 루트여야 한다. 기존 폴더에는 임의로 `git init`이나 커밋을 하지 말고 현재 Git 상태부터 확인한다.

## 계정과 실행 환경

Mac에서 Orca 앱, Claude Code, Codex, Bun, Python 3, Git이 필요하다. 이 프로그램은 Mac에서 Discord Gateway·REST에 연결하므로 별도 공개 웹 서버나 포트 포워딩을 설정하지 않는다. 자동 운영 시간에는 Mac이 켜져 있고 Orca와 네트워크가 유지되어야 한다.

Claude Channels는 research preview이며 claude.ai 또는 Anthropic Console 인증이 필요하다. 조직에서 관리한다면 `channelsEnabled` 허용 여부도 확인한다. 이 저장소의 커스텀 플러그인은 개발 채널 플래그로 실행된다. 일반 CLI 로그인 성공만으로 개발 채널 연결까지 확인된 것은 아니다. [Claude 공식 채널 문서](https://code.claude.com/docs/en/channels)

Codex는 ChatGPT 계정 또는 API 키 인증을 지원한다. 인증 저장 방식은 파일·키체인 등에 따라 다르다. **현재 자비스 구현은 파일 기반 `auth.json`을 필요로 한다.** `init`은 현재 `CODEX_HOME`의 인증 파일 경로만 `codexAuthFile`에 저장한다. 키체인만 사용하는 환경은 파일 기반 로그인 설정 또는 별도 인증 연동이 필요하다. [Codex 공식 인증 문서](https://learn.chatgpt.com/docs/auth)

```sh
claude auth status --json
codex login status
orca status --json
```

Claude가 로그아웃 상태면 `claude auth login`으로 사용자 인증을 진행한다. Codex 인증 파일이 없으면 사용할 계정으로 로그인하고 `codexAuthFile`에 실제 파일 경로를 지정한다. 기존 인증 파일 내용은 출력하지 않는다.

## Discord 설정

1. 사용할 서버에 `#기획`, `#운영-로그`, `#my-app`처럼 서로 다른 텍스트 채널 세 개를 준비한다. 사용자 설정의 개발자 모드를 켜고 서버·사용자·채널의 ID 복사 메뉴로 값을 얻는다.
2. [Discord Developer Portal](https://discord.com/developers/applications)에서 New Application으로 프라이데이·해피·마크1·자비스를 각각 만든다. General Information의 Application ID와 Bot의 Reset Token으로 얻은 토큰을 사용한다.
3. 각 앱의 Bot → Privileged Gateway Intents에서 **Message Content Intent**를 켠다. 메시지 본문을 읽는 봇에 필요한 설정이다. [Discord Gateway 문서](https://docs.discord.com/developers/events/gateway#message-content-intent)
4. 아래 명령으로 로컬에 저장하고 `invites`가 만든 URL로 같은 서버에 초대한다. 이 구현은 OAuth2 `bot` scope를 쓰며 슬래시 명령 등록은 필요 없다.

```sh
cd /Users/seunghyun/Project/orchestrator/orca-discord-orchestrator
python3 bin/setup.py bot-env 프라이데이
python3 bin/setup.py bot-env 해피
python3 bin/setup.py bot-env 마크1
python3 bin/setup.py bot-env 자비스
python3 bin/setup.py invites
```

입력한 파일은 `~/.claude/channels/bots/<봇>.env`에 권한 600으로 저장한다. 기존 파일이 있으면 덮어쓰지 않는다. 토큰 교체가 필요할 때는 봇을 종료한 뒤 해당 파일을 직접 갱신한다.

초대 도구가 설정하는 권한은 다음과 같다. 채널의 개별 권한 덮어쓰기로 봇 접근이 막혀 있으면 서버 역할 권한만으로 해결되지 않는다. 스레드 쓰기 권한은 일반 메시지 쓰기와 별도다. [Discord 권한 문서](https://docs.discord.com/developers/topics/permissions)

| 역할 | 권한 |
|---|---|
| 공통 | View Channels, Send Messages, Send Messages in Threads, Read Message History, Embed Links, Attach Files, Add Reactions |
| 프라이데이·해피 | 추가: Create Public Threads |
| 마크1 | 추가: Manage Threads — 다른 봇이 만든 작업 스레드 이름 변경·보관 |
| 자비스 | 공통 권한 |
| 프라이데이 자동 채널 생성 사용 시 | 선택: Manage Channels (`invites --manage-channels`) |

Administrator는 필요하지 않다. 처음에는 채널을 직접 만드는 편이 간단하다. 네 봇 모두 운영 로그를 쓸 수 있어야 하고, 프라이데이·해피·마크1·자비스는 프로젝트 채널에 접근할 수 있어야 한다. 프라이데이·자비스는 기획 채널도 사용한다. 수신 제한은 별도로 owner allowlist와 역할별 채널 설정에서 적용된다.

이 저장소는 `discord-orca@orca-local` 포크를 사용하고 access.json을 자동으로 만든다. 공식 Discord 플러그인의 설치·pair 명령을 이 설정에 섞지 않는다.

## 로컬 설정

이번 사용자 폴더에는 이미 `routes.json`을 생성해 두었다. 새 설치에서만 다음처럼 초기화할 수 있다.

```sh
python3 bin/setup.py init \
  --project-name my-app \
  --project-path /absolute/path/to/project \
  --guild-id 111111111111111111 \
  --owner-user-id 222222222222222222 \
  --general-channel-id 333333333333333333 \
  --ops-log-channel-id 444444444444444444 \
  --project-channel-id 555555555555555555
```

위 숫자는 예시다. 이미 있는 파일은 편집해 빈 경로와 `*_ID` 표시를 실제 값으로 교체한다. ID는 모두 문자열로 둔다. `routes.json`은 Git 추적 대상이 아니며 커밋하지 않는다.

```sh
# 현재 설치의 marketplace와 오케스트레이터 자체 등록은 완료했다.
# 실제 작업 프로젝트 경로를 확정한 뒤 다음을 수행한다.
orca repo add --path /absolute/path/to/project --json
python3 bin/setup.py doctor
```

`doctor`의 TODO를 해결한다. `--offline`은 로그인·Orca 연결 확인을 생략한다. 성공하더라도 실제 Discord 연결·모델 호출은 아직 검사하지 않았다는 뜻이다. Claude의 `opus`/`sonnet`은 별칭을 사용하며, 다른 모델을 원하면 계정에서 사용할 수 있는 식별자를 설정한다. 자비스 model/effort의 빈 값은 격리 CLI의 기본값을 뜻한다.

## 최초 사용 확인

준비가 끝나면 직접 기동한다.

```sh
bin/lead-up.sh 상담역
bin/lead-up.sh 접수원
bin/jarvis-up.sh
bin/status.sh
```

1. 기획 채널에서 짧은 질문에 프라이데이가 답하는지 확인한다.
2. 프로젝트 채널에 작은 작업을 요청한다. 해피가 스레드를 만들고 마크를 배정하는지 확인한다.
3. `state/threads/<스레드ID>.json`의 `path`가 원본 프로젝트와 다른지 확인한다. 새 worktree는 원본 HEAD 커밋 기준이다. 원본 폴더의 미커밋 파일·로컬 `.env`·설치 의존성이 필요하면 별도 worktree 준비가 필요할 수 있다.
4. 스레드 후속 요청과 `@자비스 검수`를 확인한다. 검수 결과를 보고 사용자가 수정 지시를 전달한다.
5. `종료` 후 pool이 비는지 확인한다. `이전 세션 이어서`로 같은 worktree의 재개도 확인한다.
6. 결과 브랜치를 검토한 뒤 원본에 병합할 방법을 정한다. 종료는 병합·worktree 삭제 명령이 아니다.

실제 연결이 안정적인 것을 확인한 다음 [운영 가이드](ADMIN.md)의 launchd 등록으로 자동 복구·유휴 정리를 켠다. 계정 사용량을 본 뒤 마크를 추가한다. 비전은 별도 앱·라운지·`enabled.비전=true`가 필요하며 초기 적용에는 필요 없다.

## 선택: 그록 작업자

Grok CLI로 동작하는 작업자 봇을 마크와 함께 둘 수 있다. 초기 적용에는 필요 없다. 자세한 내용은 [운영 가이드](ADMIN.md#그록-작업자-추가-선택).

```sh
grok login                      # grok.com 계정. `grok models` 첫 줄에 logged in 확인
cd grok-worker && bun install --ignore-scripts && cd ..
# Discord 앱(그록1) 생성 + Message Content Intent → routes.json bots.workers 에 {"name":"그록1","engine":"grok"} 추가
python3 bin/setup.py bot-env 그록1
python3 bin/setup.py invites
python3 bin/setup.py doctor
```

해피에게 "그록으로 …"라고 요청하면 그록 작업자가 배정된다. 기본 모델은 `models.grok작업자`(예: `grok-4.7`)다.
