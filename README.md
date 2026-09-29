[한국어](README.ko.md)

> **Note:** the bot's own Discord messages (buttons, block notices, redaction markers) are currently in Korean. Claude itself replies in the language you write in.

# claude-discord-bridge

Send a message on Discord from your phone, and Claude Code on your home or office computer executes the task and responds.

```
Phone (Mobile)          Discord           Your Computer (Home/Office)
    │                     │                          │
    │  "Fix the tests"    │                          │
    ├────────────────────>│─────────────────────────>│  Run claude CLI
    │                     │                          │
    │                     │                          │  Execute local tasks immediately
    │                     │                          │  Block external transfers (⛔)
    │                     │                          │
    │  "Fixed 3 tests"    │<─────────────────────────┤
```

**No open ports or port forwarding needed.** The computer maintains outbound connections to Discord, bypassing NAT and corporate/school firewalls seamlessly.

**Persistent context.** Each Discord thread corresponds to a dedicated Claude session. The bot remembers previous turns even after restart or hours of inactivity.

---

## Supported Environments

| OS | Supported | Notes |
|---|:---:|---|
| **macOS** | ✅ | Apple Silicon and Intel (Seatbelt-based OS sandbox) |
| **Linux** | ✅ | Ubuntu / Debian, etc. (Requires `bubblewrap` and `socat`, systemd user service) |
| **Windows (WSL2)** | ✅ | Runs inside WSL2 Ubuntu environment (systemd enabled) |
| **Windows (Native)** | ❌ | **Not supported (startup refused)**. Claude Code's sandbox only operates on macOS, Linux, and WSL2. |

---

## ⚠️ Security and Approval Model

To allow local operations (Bash, Edit, Write, etc.) to run automatically without prompting for every button click on your phone, commands are isolated inside an **OS sandbox**. In exchange, **all external outbound data paths are strictly blocked**.

| Defense Layer | Details |
|---|---|
| Owner Verification | Verifies `OWNER_ID` for message authors and interaction users |
| Auto-allow Local Work | File modification, reading, building, and testing run immediately inside the sandbox |
| Block External Leaks | Arbitrary external network connections blocked. Only git push/fetch/pull to existing remotes allowed |
| Block New Repositories | Adding new remotes (`remote add/set-url`, git clones, or pushing to new repos) is blocked |
| Package Downloads | Downloads from standard package registries are allowed for dependency installation (see table below) |
| Block Publish Commands | Package publishing (`npm publish`, `cargo publish`, `twine upload`, `docker push`, etc.) strictly blocked |
| Protect Credentials | Reading or writing `~/.ssh/**`, `~/.aws/**`, `~/.npmrc`, `~/.pypirc`, Keychains, etc., is blocked |
| Working Directory Bound | Writes outside the session working directory (`cwd`) are blocked |
| Protect Bot Code | The bot's own repository and state directory cannot be modified through the bot |
| Subagents & Skills | `Agent`, `Task`, and custom skills (`user:` plugins) are allowed under identical sandbox restrictions |
| External Delegation | When configured, external CLI agents are run as trusted processes outside the bot sandbox |
| Block Notifications | When a policy is violated, tool execution is blocked and `⛔ Blocked — <reason>` is posted to the thread |
| Redaction Filter | Passwords, tokens, keys, IPs, URL credentials, private keys, emails, phone numbers, and SSNs are replaced with a masked marker (`[가림]`, Korean for "redacted") |

### Allowed Package Registry Domains

Downloads for dependency installation (`npm install`, `pip install`, etc.) are allowed for the following domains:
- `registry.npmjs.org`, `registry.yarnpkg.com`
- `pypi.org`, `files.pythonhosted.org`
- `repo.maven.apache.org`, `repo1.maven.org`, `plugins.gradle.org`, `plugins-artifacts.gradle.org`, `services.gradle.org`, `downloads.gradle.org`, `dl.google.com`, `maven.google.com`
- `crates.io`, `index.crates.io`, `static.crates.io`
- `rubygems.org`
- `proxy.golang.org`, `sum.golang.org`

*(Note: Domains such as github.com, api.github.com, and gist.github.com are blocked to prevent code exfiltration.)*

### ⚠️ Remaining Risks (Must Read)

1. **External agents (if configured) run outside the bot's sandbox.** Isolation during external agent execution relies on the agent's own sandboxing and permission controls. The bot cannot prevent data leakage through the agent's external MCP tools (e.g. Slack, Notion) or web access.
2. **Pushing to existing remotes**: Committing and pushing secrets or private data to an existing legitimate remote (e.g., a public GitHub repository) cannot be prevented.
3. **Registry upload risks**: Registries like npm, crates.io, and rubygems share the same domains for download and upload. While primary command filters block common publish tools, scripts could bypass them if attacker credentials are provided.
4. **`SANDBOX_ALLOWED_DOMAINS` additions**: Any domain added to this list will be allowed by the sandbox network proxy.
5. **Discord account compromise**: If your Discord account is compromised, the attacker gains the same local execution, git push, and delegation privileges.
6. **Regex-based redaction**: Unconventional password formats appearing without known key identifiers may escape regex matching.

---

## Operating Modes

### 1. Claude-Only Mode (Default)
Active when `DELEGATE_*` variables are unset.
The delegation path is disabled. Any CLI invocation commands proposed by the model are treated as standard Bash commands and executed within the sandbox without host privileges.

### 2. Claude + External Agent Mode
When `DELEGATE_NAME` is configured, Claude can delegate complex coding tasks to external CLI agents (such as Antigravity, Codex, Gemini, etc.).

| Variable | Required | Description |
|---|---|---|
| `DELEGATE_NAME` | Required | Command invocation word used by Claude (`^[A-Za-z0-9][A-Za-z0-9._-]*$`). E.g., `agy`, `codex`, `gemini` |
| `DELEGATE_BIN` | Required | Absolute executable binary path (supports `~`) |
| `DELEGATE_ARGS` | Required | Argument template string containing exactly one `{prompt}` token (no partial substrings) |
| `DELEGATE_TIMEOUT_S` | Optional | Process timeout in seconds (Default: `3000`, must be between 0 and 5400) |
| `DELEGATE_PROTECT_PATHS` | Optional | Comma-separated paths to protect from Edit tool modifications (e.g., `~/.codex`) |

**How It Works:**
- When Claude executes a standalone command matching `<name> -p '<task prompt>'` or `<name> --prompt '<task prompt>'`, the permission gate intercepts it.
- The model cannot add or modify flags (flags are enforced strictly by the operator via `DELEGATE_ARGS`).
- Upon verification, the bot executes `DELEGATE_BIN` with `DELEGATE_ARGS` outside the sandbox and pipes output back to Claude.
- Pipes (`|`), redirects (`>`), chained commands (`;`, `&&`), or background execution are immediately blocked.
- A prompt that is empty or starts with `-` is rejected, so the model cannot smuggle an agent flag into the `{prompt}` slot.

**Risk Notice:**
- External agents execute with host permissions outside the bot sandbox. Ensure you include the agent's native sandbox flags in `DELEGATE_ARGS`.
- The bot cannot intercept data transmitted by the external agent's own network or MCP integrations.

**Skill Discovery:**
- `~/.claude/skills` and `~/.claude/agents` are exposed as a local `user:` plugin.
- Global MCPs, hooks, settings.json, and global CLAUDE.md are not loaded (only the repository's CLAUDE.md is loaded). Skills dependent on web/network access will fail in the sandbox.

#### Example Presets

> ⚠️ **Warning**: CLI flags vary across tool versions. Verify supported flags with `--help` before use.

```bash
# Antigravity Preset
DELEGATE_NAME=agy
DELEGATE_BIN=~/.local/bin/agy
DELEGATE_ARGS=--sandbox --dangerously-skip-permissions -p {prompt}
DELEGATE_PROTECT_PATHS=~/.antigravity

# OpenAI Codex CLI Preset
DELEGATE_NAME=codex
DELEGATE_BIN=/usr/local/bin/codex
DELEGATE_ARGS=exec --sandbox workspace-write {prompt}
DELEGATE_PROTECT_PATHS=~/.codex

# Gemini CLI Preset
DELEGATE_NAME=gemini
DELEGATE_BIN=/usr/local/bin/gemini
DELEGATE_ARGS=--sandbox --yolo -p {prompt}
DELEGATE_PROTECT_PATHS=~/.gemini
```

---

## Installation & Setup

### Step 1: Create a Discord Bot

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Navigate to **Bot** tab → **Add Bot**.
3. ⚠️ **Enable MESSAGE CONTENT INTENT**. (If disabled, the bot receives empty messages and will not respond.)
4. Click **Reset Token** and copy the bot token securely.
5. Under **OAuth2 → URL Generator**:
   - scopes: `bot`
   - permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Attach Files`, `Read Message History`
6. Open the generated URL in a browser and invite the bot to your private server.

### Step 2: Retrieve Discord IDs

Enable **Settings → Advanced → Developer Mode** in Discord:
- **Your User ID**: Right-click your username → "Copy User ID"
- **Server ID**: Right-click your server icon → "Copy Server ID"

### Step 3: Configure Environment (`~/.claude-discord/.env`)

```bash
mkdir -p ~/.claude-discord
cat > ~/.claude-discord/.env <<'ENVEOF'
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=111111111111111111
GUILD_IDS=222222222222222222
DEFAULT_CWD=~/
IDLE_TIMEOUT_HOURS=2
APPROVAL_TIMEOUT_S=120
# Additional package domains (optional, comma-separated)
# SANDBOX_ALLOWED_DOMAINS=custom-repo.example.com
# External delegation (optional)
# DELEGATE_NAME=codex
# DELEGATE_BIN=/usr/local/bin/codex
# DELEGATE_ARGS=exec --sandbox workspace-write {prompt}
# DELEGATE_TIMEOUT_S=3000
# DELEGATE_PROTECT_PATHS=~/.codex
ENVEOF
chmod 600 ~/.claude-discord/.env
```

---

## Deployment by Platform

### macOS

Uses macOS-specific launchd plists and the `dicobot` CLI tool.

```bash
# 1. Install dependencies
uv venv --python 3.13
uv pip install -r requirements.txt

# 2. Register launchd service
sed -e "s|__REPO_DIR__|$PWD|g" -e "s|__HOME_DIR__|$HOME|g" \
  deploy/com.claude-discord.plist > ~/Library/LaunchAgents/com.claude-discord.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-discord.plist

# 3. Install management CLI (macOS only)
./deploy/install-cli.sh
```

CLI Management Commands:
- `dicobot`: Check service status
- `dicobot start` / `stop` / `restart`: Start, stop, or restart the service
- `dicobot tail`: Stream real-time logs

---

### Linux

Requires `bubblewrap` and `socat` for sandboxing. Deployed as a systemd user unit.

```bash
# 1. Install packages (Ubuntu/Debian)
sudo apt update && sudo apt install -y bubblewrap socat

# 2. Setup venv and dependencies
uv venv --python 3.13
uv pip install -r requirements.txt

# 3. Install systemd user service
mkdir -p ~/.config/systemd/user
sed -e "s#__REPO_DIR__#$PWD#g" -e "s#__HOME_DIR__#$HOME#g" \
  deploy/linux/claude-discord.service > ~/.config/systemd/user/claude-discord.service

# 4. Enable and start unit
systemctl --user daemon-reload
systemctl --user enable --now claude-discord
loginctl enable-linger "$USER"   # Persist after logout

# 5. Monitor logs
journalctl --user -u claude-discord -f
```

---

### Windows (WSL2)

Claude Code's sandbox needs macOS Seatbelt or Linux Bubblewrap. **Native Windows is not supported and startup will be aborted.** Run inside WSL2.

1. **Install WSL2**:
   Run in an administrator PowerShell, then reboot:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. **Enable systemd in WSL**:
   Inside your WSL terminal, add the following to `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   ```
   Run `wsl --shutdown` from PowerShell and reopen WSL.
3. **Install Claude Code CLI in WSL**:
   Install Claude CLI inside the WSL Ubuntu environment and authenticate with `claude login`.
4. **Repository Location**:
   Clone the repository into the **WSL filesystem (`~/...`)**, NOT into Windows mounts (`/mnt/c/...`), to avoid permission, performance, and sandboxing failures.
5. **Install Service**:
   Follow steps 1-5 in the **Linux** instructions above.
6. **Autostart on Windows Logon (Optional)**:
   Register a task in Windows Task Scheduler triggered "At log on" running `wsl.exe -d Ubuntu --exec /bin/true`. This spins up WSL in the background, allowing systemd to start the bridge. *(Verify behavior for your setup)*
7. **Why Native Windows is Rejected**:
   Native Windows lacks the Bubblewrap container backend, which would allow unsandboxed command execution on your host. For safety, the bot terminates immediately on Windows.

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `DISCORD_TOKEN` | Yes | - | Discord bot token |
| `OWNER_ID` | Yes | - | Numeric Discord snowflake ID of the bot owner |
| `GUILD_IDS` | Recommended | (empty) | Allowed Discord server/guild IDs (comma-separated) |
| `CHANNEL_IDS` | Optional | (empty) | Restrict to specific channel IDs (empty processes all channels) |
| `STATE_DIR` | Optional | `~/.claude-discord` | Directory storing session maps and logs |
| `DEFAULT_CWD` | Optional | `~` | Default starting directory for sessions |
| `IDLE_TIMEOUT_HOURS` | Optional | `2.0` | Hours of inactivity before closing client connection |
| `APPROVAL_TIMEOUT_S` | Optional | `120.0` | Timeout in seconds for interactive tool approval prompts |
| `SANDBOX_ALLOWED_DOMAINS` | Optional | (empty) | Custom package domains to permit (comma-separated) |
| `DELEGATE_NAME` | Optional | - | External agent invocation command name (e.g. `codex`) |
| `DELEGATE_BIN` | If delegated | - | Absolute path to external agent binary |
| `DELEGATE_ARGS` | If delegated | - | Argument template containing `{prompt}` |
| `DELEGATE_TIMEOUT_S` | Optional | `3000.0` | Execution timeout in seconds (must be < 5400) |
| `DELEGATE_PROTECT_PATHS` | Optional | - | Paths protected from Edit modifications (comma-separated) |

---

## Usage

### Discord Commands

- `!new <path>`: Create a thread and start a session rooted at the given path
- `!cd <path>`: Switch directory for the thread (starts a new session)
- `!resume <session_id>`: Resume an existing session ID
- `!stop`: Interrupt current in-flight turn
- `!sessions`: List active sessions

Standard conversation messages are forwarded to Claude.

### Multi-Device Channel Routing

Use one bot token across multiple devices by filtering by channel:

```bash
# Laptop A (.env)
CHANNEL_IDS=111111111111111111  # #laptop-a

# Desktop B (.env)
CHANNEL_IDS=222222222222222222  # #desktop-b
```

Each host only responds to threads initiated in its assigned channel.

---

## Troubleshooting

### Bot Does Not Respond
1. Check that **MESSAGE CONTENT INTENT** is enabled on Discord Developer Portal.
2. Confirm `OWNER_ID`, `GUILD_IDS`, and `CHANNEL_IDS` match your Discord IDs.

### Viewing Logs
- **macOS**: `dicobot tail` or `tail -f ~/.claude-discord/bot.log`
- **Linux / WSL2**: `journalctl --user -u claude-discord -f`

---

## Updating

```bash
cd ~/claude-discord-bridge
git pull

# macOS
dicobot restart

# Linux / WSL2
systemctl --user restart claude-discord
```

---

## Testing

```bash
.venv/bin/pytest -q
.venv/bin/pytest -m integration -q
```

---

## File Locations

| File | Purpose |
|---|---|
| `~/.claude-discord/.env` | Environment configuration and token (chmod 600) |
| `~/.claude-discord/threads.json` | Thread-to-session mapping database |
| `~/.claude-discord/audit.log` | Forensic audit log of tool decisions |
| `~/.claude-discord/bot.log` | Standard output log (macOS) |
| `journald` | Service log (Linux / WSL2) |

## License

MIT — see [LICENSE](LICENSE).
