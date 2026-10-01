[English](README.md)

# claude-discord-bridge

휴대폰에서 디스코드로 메시지를 보내면, 집이나 사무실 컴퓨터에서 Claude Code가 그 일을 하고 답을 보내옵니다.

```
휴대폰 (LTE)          디스코드           내 컴퓨터 (집/사무실)
    │                   │                    │
    │     "테스트 고쳐줘"  │                    │
    ├──────────────────>│───────────────────>│  claude CLI 실행
    │                   │                    │
    │                   │                    │  로컬 작업은 바로 실행
    │                   │                    │  외부 전송은 차단(⛔)
    │                   │                    │
    │     "3개 고쳤습니다" │<───────────────────┤
```

**공유기에 포트를 열지 않습니다.** 컴퓨터가 디스코드로 나가는 아웃바운드 연결만 쓰기 때문에 NAT도 회사·학교 방화벽도 그대로 통과합니다.

**대화가 이어집니다.** 디스코드 쓰레드 하나가 Claude 세션 하나입니다. 봇을 재시작해도, 두 시간 뒤에 다시 물어봐도 앞 대화를 기억합니다.

---

## 지원 환경

| OS | 지원 여부 | 비고 |
|---|:---:|---|
| **macOS** | ✅ | Apple Silicon / Intel 지원 (Seatbelt 기반 샌드박스) |
| **Linux** | ✅ | Ubuntu / Debian 등 (`bubblewrap`, `socat` 필요, systemd 사용자 서비스) |
| **Windows (WSL2)** | ✅ | WSL2 내부 Ubuntu 환경에서 실행 (systemd 활성화 필요) |
| **Windows (Native)** | ❌ | **미지원 (기동 거부)**. Claude Code 샌드박스는 macOS, Linux, WSL2에서만 동작합니다. |

---

## ⚠️ 보안 및 승인 모델

휴대폰에서 승인 버튼을 일일이 누르지 않아도 로컬 작업(Bash, Edit, Write 등)이 자동으로 진행되도록 **OS 샌드박스** 안에서 격리 실행됩니다. 대신 **외부로 데이터가 나가는 경로는 전부 차단**됩니다.

| 방어 계층 | 내용 |
|---|---|
| 본인만 사용 | 메시지 작성자와 상호작용자 모두 소유자 ID(`OWNER_ID`) 검증 |
| 로컬 자동 허용 | 파일 수정, 읽기, 빌드, 테스트 등 내부 작업은 샌드박스 안에서 즉시 자동 실행 |
| 외부 유출 원천 차단 | 외부 네트워크 접속 차단. 오직 기존에 등록된 git remote로의 push/fetch/pull만 허용 |
| 새 저장소 연결 차단 | 새 repo 연결(`remote add/set-url`, 새 git clone/repo 생성 후 푸시) 차단 |
| 패키지 다운로드 허용 | 의존성 설치를 위한 기본 패키지 레지스트리 다운로드 허용 (아래 표 참조) |
| 배포 명령 차단 | `npm publish`, `cargo publish`, `twine upload`, `docker push` 등 배포 명령 원천 차단 |
| 자격증명 보호 | `~/.ssh/**`, `~/.aws/**`, `~/.npmrc`, `~/.pypirc`, 키체인 등 민감 파일 읽기/쓰기 차단 |
| 작업 폴더 격리 | 세션 작업 폴더(`cwd`) 밖 파일 쓰기 차단 |
| 봇 저장소 보호 | 봇 자신의 소스 코드 및 상태 폴더는 봇을 통해 수정 불가 |
| 하위 에이전트 & 스킬 | `Agent`, `Task`, 사용자 정의 스킬(`user:` 플러그인) 사용 가능 (동일한 샌드박스 및 차단 적용) |
| 외부 에이전트 위임 | 외부 에이전트 설정 시 봇이 신뢰 프로세스로 직접 실행 |
| 차단 알림 | 규칙 위반 시 도구 실행이 차단되고 스레드에 `⛔ 차단됨 — <이유>` 한 줄 알림 |
| 디스코드 출력 가림 | 봇이 보내는 모든 메시지·첨부에서 비밀번호·토큰·키·IP·URL 자격증명·개인키·이메일·전화번호·주민번호·카드번호를 `[가림]`으로 바꿔 보냄 |

### 허용된 기본 패키지 레지스트리 도메인

의존성 설치(`npm install`, `pip install` 등)를 위해 아래 도메인의 다운로드가 기본 허용됩니다:
- `registry.npmjs.org`, `registry.yarnpkg.com`
- `pypi.org`, `files.pythonhosted.org`
- `repo.maven.apache.org`, `repo1.maven.org`, `plugins.gradle.org`, `plugins-artifacts.gradle.org`, `services.gradle.org`, `downloads.gradle.org`, `dl.google.com`, `maven.google.com`
- `crates.io`, `index.crates.io`, `static.crates.io`
- `rubygems.org`
- `proxy.golang.org`, `sum.golang.org`

*(주의: github.com, api.github.com, gist.github.com 등 소스 유출 경로가 될 수 있는 도메인은 차단됩니다.)*

### ⚠️ 남는 위험 (반드시 인지할 것)

1. **외부 에이전트(설정한 경우)는 샌드박스 밖에서 실행됩니다.** 외부 에이전트 실행 동안의 격리는 해당 에이전트 자체의 샌드박스 및 승인 설정에 의존합니다. 에이전트에 연결된 외부 MCP(Slack, Notion 등)나 웹 도구로의 유출은 봇이 막지 못합니다.
2. **허용된 기존 remote로의 push**: 허용된 정상 remote(예: 공개 GitHub 저장소)에 시크릿이나 개인정보를 커밋하여 push하는 행위는 차단할 수 없습니다.
3. **패키지 레지스트리 업로드 위험**: npm, crates.io, rubygems 등은 다운로드와 업로드 도메인이 같습니다. 배포 명령 1차 필터가 있지만 우회 가능성이 있으며, 사용자가 직접 공격자 토큰을 설정하면 배포될 수 있습니다.
4. **`SANDBOX_ALLOWED_DOMAINS`에 추가한 도메인**: 추가로 허용한 도메인으로의 데이터 전송은 샌드박스 프록시가 허용합니다.
5. **디스코드 계정 탈취**: 계정이 탈취되면 공격자도 동일한 로컬 실행 및 기존 remote push, 외부 에이전트 위임 권한을 갖습니다.
6. **가림은 정규식 기반**: 키 이름 없이 등장하는 임의 문자열 비밀번호처럼 알려지지 않은 형식은 통과할 수 있습니다.

---

## 두 가지 모드

### 1. Claude 단독 모드 (기본값)
`DELEGATE_*` 환경변수를 설정하지 않으면 이 모드로 동작합니다.
외부 에이전트 위임 코드 경로가 비활성화되며, 모델이 CLI 에이전트 호출 명령을 내려도 일반 Bash 명령으로 취급되어 샌드박스 안에서만 실행됩니다.

### 2. Claude + 외부 에이전트 조합 모드
`DELEGATE_NAME`을 설정하면 외부 CLI 코딩 에이전트(Antigravity, Codex, Gemini 등)에게 작업을 위임할 수 있습니다.

| 변수 | 필수 | 설명 |
|---|---|---|
| `DELEGATE_NAME` | 필수 | Claude가 칠 명령 단어 (정규식 `^[A-Za-z0-9][A-Za-z0-9._-]*$`). 예: `agy`, `codex`, `gemini` |
| `DELEGATE_BIN` | 필수 | 실행 파일 경로 (`~` 허용, 절대경로여야 함) |
| `DELEGATE_ARGS` | 필수 | 실행 인자 템플릿. 정확히 1개의 `{prompt}` 토큰을 포함해야 함 (부분 문자열 불가) |
| `DELEGATE_TIMEOUT_S` | 선택 | 위임 프로세스 타임아웃 (기본값: `3000`, 0 초과 5400 미만) |
| `DELEGATE_PROTECT_PATHS` | 선택 | Edit deny로 보호할 에이전트 설정 경로 (쉼표 구분, 예: `~/.codex`) |

**동작 방식:**
- Claude가 `<name> -p '<task prompt>'` 또는 `<name> --prompt '<task prompt>'` 형식의 단독 Bash 명령을 실행하면, 봇 권한 게이트가 이를 감지합니다.
- 모델은 인자나 플래그를 임의로 추가하거나 변경할 수 없습니다 (플래그는 운영자가 지정한 `DELEGATE_ARGS`로 고정).
- 검증을 통과하면 봇 프로세스가 직접 샌드박스 밖에서 `DELEGATE_BIN`과 `DELEGATE_ARGS`({prompt} 치환)로 실행하고, 실행 결과를 샌드박스 내부의 Claude에게 반환합니다.
- 파이프(`|`), 리다이렉트(`>`), 세미콜론(`;`) 등이 섞여 있으면 즉시 차단됩니다.
- 프롬프트가 비어 있거나 `-`로 시작하면 거부합니다. 모델이 `{prompt}` 자리에 에이전트 플래그를 끼워 넣지 못하게 하기 위해서입니다.

**위험성 안내:**
- 외부 에이전트는 봇 샌드박스 밖에서 호스트 권한으로 실행됩니다. 따라서 에이전트 자체의 샌드박스 플래그나 권한 설정을 `DELEGATE_ARGS`에 반드시 지정하세요.
- 외부 에이전트가 자체 MCP나 네트워크를 통해 데이터를 외부로 전송하는 것은 봇이 차단할 수 없습니다.

**스킬 로딩:**
- `~/.claude/skills` 및 `~/.claude/agents`가 로컬 `user:` 플러그인으로 로드됩니다.
- 전역 MCP, hooks, settings.json, 전역 CLAUDE.md는 로드되지 않으며(작업 디렉터리의 CLAUDE.md만 반영), 웹/네트워크에 의존하는 스킬은 샌드박스에 의해 차단됩니다.

#### 예시 프리셋

> ⚠️ **경고**: CLI 플래그는 버전에 따라 달라질 수 있으므로, 사용 전 각 도구의 `--help`를 확인하세요.

```bash
# Antigravity 예시
DELEGATE_NAME=agy
DELEGATE_BIN=~/.local/bin/agy
DELEGATE_ARGS=--sandbox --dangerously-skip-permissions -p {prompt}
DELEGATE_PROTECT_PATHS=~/.antigravity

# OpenAI Codex CLI 예시
DELEGATE_NAME=codex
DELEGATE_BIN=/usr/local/bin/codex
DELEGATE_ARGS=exec --sandbox workspace-write {prompt}
DELEGATE_PROTECT_PATHS=~/.codex

# Gemini CLI 예시
DELEGATE_NAME=gemini
DELEGATE_BIN=/usr/local/bin/gemini
DELEGATE_ARGS=--sandbox --yolo -p {prompt}
DELEGATE_PROTECT_PATHS=~/.gemini
```

---

## 설치 및 설정

### 1단계: Discord 봇 생성

1. https://discord.com/developers/applications 접속 후 **New Application** 생성.
2. 좌측 **Bot** 탭 → **Add Bot**.
3. ⚠️ **MESSAGE CONTENT INTENT** 토글을 반드시 켭니다. (꺼져 있으면 봇이 메시지를 읽지 못합니다.)
4. **Reset Token** 클릭 후 토큰을 복사해 둡니다.
5. **OAuth2 → URL Generator**:
   - scopes: `bot`
   - permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Attach Files`, `Read Message History`
6. 생성된 URL로 본인 전용 비공개 서버에 봇을 초대합니다.

### 2단계: ID 확인

디스코드 **설정 → 고급 → 개발자 모드**를 켠 후:
- **내 사용자 ID**: 내 프로필 우클릭 → "사용자 ID 복사"
- **서버 ID**: 서버 이름 우클릭 → "서버 ID 복사"

### 3단계: 환경변수 설정 (`~/.claude-discord/.env`)

```bash
mkdir -p ~/.claude-discord
cat > ~/.claude-discord/.env <<'ENVEOF'
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=111111111111111111
GUILD_IDS=222222222222222222
DEFAULT_CWD=~/
IDLE_TIMEOUT_HOURS=2
APPROVAL_TIMEOUT_S=120
# 추가 허용 패키지/다운로드 도메인 (선택, 쉼표 구분)
# SANDBOX_ALLOWED_DOMAINS=custom-repo.example.com
# 외부 에이전트 위임 설정 (선택)
# DELEGATE_NAME=codex
# DELEGATE_BIN=/usr/local/bin/codex
# DELEGATE_ARGS=exec --sandbox workspace-write {prompt}
# DELEGATE_TIMEOUT_S=3000
# DELEGATE_PROTECT_PATHS=~/.codex
ENVEOF
chmod 600 ~/.claude-discord/.env
```

---

## OS별 설치 및 실행

### macOS

macOS 전용 실행 도구(`dicobot`, launchd plist)를 사용할 수 있습니다.

```bash
# 1. 의존성 설치
uv venv --python 3.13
uv pip install -r requirements.txt

# 2. launchd 등록
sed -e "s|__REPO_DIR__|$PWD|g" -e "s|__HOME_DIR__|$HOME|g" \
  deploy/com.claude-discord.plist > ~/Library/LaunchAgents/com.claude-discord.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-discord.plist

# 3. CLI 관리 도구 등록 (macOS 전용)
./deploy/install-cli.sh
```

관리 명령:
- `dicobot`: 상태 확인
- `dicobot start` / `stop` / `restart`: 시작/정지/재시작
- `dicobot tail`: 실시간 로그 확인

---

### Linux

Linux에서는 샌드박스를 위해 `bubblewrap`과 `socat`이 필요하며, systemd 사용자 유닛으로 등록합니다.

```bash
# 1. 필수 패키지 설치 (Ubuntu/Debian)
sudo apt update && sudo apt install -y bubblewrap socat

# 2. 가상환경 및 의존성 설치
uv venv --python 3.13
uv pip install -r requirements.txt

# 3. systemd 사용자 서비스 설치
mkdir -p ~/.config/systemd/user
sed -e "s#__REPO_DIR__#$PWD#g" -e "s#__HOME_DIR__#$HOME#g" \
  deploy/linux/claude-discord.service > ~/.config/systemd/user/claude-discord.service

# 4. 서비스 활성화 및 시작
systemctl --user daemon-reload
systemctl --user enable --now claude-discord
loginctl enable-linger "$USER"   # 세션 로그아웃 후에도 서비스 유지

# 5. 로그 확인
journalctl --user -u claude-discord -f
```

---

### Windows (WSL2)

Claude Code의 샌드박스는 macOS Seatbelt 또는 Linux Bubblewrap이 필요하므로, **네이티브 Windows 환경에서는 봇 기동이 거부됩니다.** 반드시 WSL2 내부에서 실행해야 합니다.

1. **WSL2 설치**:
   PowerShell(관리자 권한)에서 실행 후 시스템을 재부팅합니다:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. **WSL systemd 활성화**:
   WSL 터미널 안에서 `/etc/wsl.conf` 파일에 다음 내용을 추가합니다:
   ```ini
   [boot]
   systemd=true
   ```
   PowerShell에서 `wsl --shutdown` 후 WSL에 재진입합니다.
3. **Claude CLI 설치**:
   Windows 호스트와 별개로, **WSL2 Ubuntu 내부**에 Claude Code CLI를 설치하고 로그인(`claude login`)합니다.
4. **저장소 위치**:
   저장소는 Windows 드라이브(`/mnt/c/...`)가 아닌 **WSL 내부 파일시스템(`~/...`)**에 클론해야 합니다. (권한, 성능 및 샌드박스 격리 문제)
5. **서비스 설치**:
   위의 **Linux** 설치 절차(1~5번)를 그대로 진행합니다.
6. **Windows 부팅 시 자동 시작 (선택 사항)**:
   Windows 작업 스케줄러에 "사용자 로그온 시" 트리거로 `wsl.exe -d Ubuntu --exec /bin/true`를 등록하면, Windows 로그인 시 백그라운드에서 WSL이 시작되면서 systemd 유닛(`claude-discord`)이 자동 구동됩니다. *(환경에 따라 동작 확인 필요)*
7. **네이티브 Windows 기동 거부 이유**:
   Windows 네이티브 환경에는 Bubblewrap 샌드박스 백엔드가 없어 셸 명령이 격리 없이 호스트에서 바로 실행될 위험이 있습니다. 봇은 안전을 위해 Windows 환경 감지 시 즉시 기동을 중단합니다.

---

## 전체 설정 변수

| 변수 | 필수 | 기본값 | 설명 |
|---|:---:|---|---|
| `DISCORD_TOKEN` | 필수 | - | 디스코드 봇 토큰 |
| `OWNER_ID` | 필수 | - | 봇 소유자 디스코드 Snowflake ID (숫자) |
| `GUILD_IDS` | 권장 | (비어있음) | 허용 서버 ID (쉼표 구분) |
| `CHANNEL_IDS` | 선택 | (비어있음) | 특정 기기 전용 채널 ID (비워두면 모든 채널 허용) |
| `STATE_DIR` | 선택 | `~/.claude-discord` | 세션 및 상태 파일 저장 경로 |
| `DEFAULT_CWD` | 선택 | `~` | 세션 기본 시작 경로 |
| `IDLE_TIMEOUT_HOURS` | 선택 | `2.0` | 유휴 세션 연결 정리 시간 (시간 단위) |
| `APPROVAL_TIMEOUT_S` | 선택 | `120.0` | 대화형 승인 대기 타임아웃 (초 단위) |
| `SANDBOX_ALLOWED_DOMAINS` | 선택 | (비어있음) | 기본 레지스트리 외 추가 허용 도메인 (쉼표 구분) |
| `DELEGATE_NAME` | 선택 | - | 외부 위임 에이전트 이름 (`codex`, `gemini` 등) |
| `DELEGATE_BIN` | 조합 시 필수 | - | 외부 위임 에이전트 실행 바이너리 절대경로 |
| `DELEGATE_ARGS` | 조합 시 필수 | - | 위임 실행 인자 템플릿 (정확히 1개의 `{prompt}` 포함) |
| `DELEGATE_TIMEOUT_S` | 선택 | `3000.0` | 위임 실행 타임아웃 (초 단위, 최대 5400초 미만) |
| `DELEGATE_PROTECT_PATHS` | 선택 | - | 변조 방지 보호할 에이전트 설정 경로 (쉼표 구분) |

---

## 사용법

### 디스코드 명령어

- `!new <경로>`: 새 스레드를 만들고 지정한 경로에서 새 세션 시작
- `!cd <경로>`: 현재 스레드의 작업 디렉터리 변경 (새 세션 생성)
- `!resume <세션ID>`: 이전 세션 복구
- `!stop`: 현재 실행 중인 작업 중단
- `!sessions`: 활성 세션 목록 조회

명령어가 아닌 일반 대화는 Claude에게 전달됩니다.

### 여러 기기 채널 분리

봇 토큰을 공유하면서 기기별로 전담 채널을 지정할 수 있습니다:

```bash
# 노트북 A (.env)
CHANNEL_IDS=111111111111111111  # #laptop-a

# 데스크톱 B (.env)
CHANNEL_IDS=222222222222222222  # #desktop-b
```

각 기기는 자신의 채널에서 생성된 스레드만 처리합니다.

---

## 문제 해결

### 봇이 메시지에 반응하지 않음
1. **MESSAGE CONTENT INTENT**가 켜져 있는지 확인하세요.
2. `OWNER_ID`, `GUILD_IDS`, `CHANNEL_IDS` 설정이 올바른지 확인하세요.

### 로그 확인
- **macOS**: `dicobot tail` 또는 `tail -f ~/.claude-discord/bot.log`
- **Linux / WSL2**: `journalctl --user -u claude-discord -f`

---

## 업데이트

```bash
cd ~/claude-discord-bridge
git pull

# macOS
dicobot restart

# Linux / WSL2
systemctl --user restart claude-discord
```

---

## 테스트

```bash
.venv/bin/pytest -q
.venv/bin/pytest -m integration -q
```

---

## 파일 위치

| 파일 | 내용 |
|---|---|
| `~/.claude-discord/.env` | 봇 설정 및 토큰 (권한: 600) |
| `~/.claude-discord/threads.json` | 스레드 ↔ 세션 ID 매핑 |
| `~/.claude-discord/audit.log` | 도구 실행 및 차단 감사 로그 |
| `~/.claude-discord/bot.log` | 표준 로그 (macOS) |
| `journald` | 표준 로그 (Linux / WSL2) |

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
