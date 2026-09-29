"""claude_client_factory 자체를 검증한다 (실제 CLI는 붙이지 않는다).

connect()를 호출하지 않으므로 CLI 프로세스는 뜨지 않는다 -- 토큰 비용 없는
단위 테스트. 실제 CLI를 구동하는 통합 검증은 tests/test_integration.py에 있다.
"""

from bridge.session import claude_client_factory


async def test_invalid_utf8_claude_md_does_not_raise(tmp_path):
    """CLAUDE.md에 잘못된 인코딩 바이트가 있어도 세션 생성이 죽지 않아야 한다.

    UnicodeDecodeError는 OSError의 하위 클래스가 아니어서, 예전 코드의
    `except OSError`로는 잡히지 않고 acquire()까지 그대로 전파되어 그 cwd의
    모든 세션을 깨뜨렸다 (실제 리뷰에서 재현된 버그).
    """
    (tmp_path / "CLAUDE.md").write_bytes(b"before \xff\xfe after")

    client = await claude_client_factory(tmp_path, resume=None)

    system_prompt = client.options.system_prompt
    assert system_prompt is not None
    appended = system_prompt["append"]
    # Valid text surrounding the bad bytes still comes through -- a few
    # corrupt bytes degrade the file rather than discarding it outright.
    assert "before" in appended
    assert "after" in appended


async def test_security_critical_options_are_pinned(tmp_path):
    """The branch's most important security properties, asserted rather than
    merely measured once during review.

    `setting_sources=[]` keeps a `permissions.allow` entry in any settings
    file -- including a `.claude/settings.json` committed to a cloned repo --
    from bypassing the approval gate. `mcp_servers={}` plus
    `strict_mcp_config=True` keep a project-scoped `.mcp.json` from making
    the CLI spawn an arbitrary command as a child process at session
    startup, which happens before any tool call exists for `can_use_tool` to
    gate.

    Measured on the installed CLI (2.1.270): `setting_sources=[]` alone
    already suppressed a project `.mcp.json` (a control run with
    `setting_sources=["project"]` did spawn it), so the MCP pin is defence
    in depth rather than a live hole today. It is exactly the kind of
    default a CLI version bump can flip silently, which is why it belongs in
    an assertion and not only in a comment.
    """
    options = (await claude_client_factory(tmp_path, resume=None)).options

    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True


# --- Task 3: Sandbox, Deny rules, and User plugins --------------------------


import json
from pathlib import Path
from bridge.config import Delegate
from bridge.session import DEFAULT_PACKAGE_DOMAINS, build_deny_rules, ensure_user_plugin


async def test_sandbox_options_are_pinned(tmp_path):
    client = await claude_client_factory(tmp_path, resume=None)
    sandbox = client.options.sandbox
    assert sandbox is not None
    assert sandbox["enabled"] is True
    assert sandbox["autoAllowBashIfSandboxed"] is False
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["failIfUnavailable"] is True


def test_default_package_domains_pinned():
    domains = DEFAULT_PACKAGE_DOMAINS
    assert "registry.npmjs.org" in domains
    assert "pypi.org" in domains
    assert "crates.io" in domains
    assert "rubygems.org" in domains
    assert "repo1.maven.org" in domains

    # Pin: these must NOT be present
    for forbidden in ("github.com", "api.github.com", "gist.github.com", "upload.pypi.org"):
        assert forbidden not in domains


async def test_allowed_domains_merging(tmp_path):
    client = await claude_client_factory(tmp_path, resume=None)
    assert client.options.sandbox["network"]["allowedDomains"] == sorted(DEFAULT_PACKAGE_DOMAINS)

    client_custom = await claude_client_factory(
        tmp_path,
        resume=None,
        allowed_domains=["nexus.example.com"],
    )
    expected = sorted(DEFAULT_PACKAGE_DOMAINS | {"nexus.example.com"})
    assert client_custom.options.sandbox["network"]["allowedDomains"] == expected


async def test_settings_deny_rules_and_no_allow(tmp_path):
    state_dir = tmp_path / "state"
    client = await claude_client_factory(tmp_path, resume=None, state_dir=state_dir)
    settings = json.loads(client.options.settings)

    perms = settings.get("permissions", {})
    assert "allow" not in perms
    deny = perms.get("deny", [])

    assert "Edit(//**/.git/config)" in deny
    assert "Edit(//**/.git/hooks/**)" in deny
    assert "Read(~/.ssh/**)" in deny
    assert "Read(~/.npmrc)" in deny
    assert "Edit(~/.claude/**)" in deny
    assert "Edit(~/.gemini/**)" in deny

    repo_root = Path(__file__).resolve().parents[1]
    assert f"Edit(/{repo_root}/**)" in deny
    assert f"Read(/{state_dir.resolve()}/**)" in deny
    assert f"Edit(/{state_dir.resolve()}/**)" in deny

    assert not any(rule == "Agent" or rule.startswith("Agent(") for rule in deny)
    assert not any(rule == "Task" or rule.startswith("Task(") for rule in deny)


def test_build_deny_rules_delegate_none(tmp_path):
    deny = build_deny_rules(tmp_path, delegate=None)
    forbidden = "".join(["anti", "gravity"])
    assert not any(forbidden in rule for rule in deny)


def test_build_deny_rules_with_delegate(tmp_path):
    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("-p", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=("~/.codex", "~/.gemini"),
    )
    deny = build_deny_rules(tmp_path, delegate=delegate)
    assert "Edit(//opt/codex)" in deny
    assert "Edit(~/.codex/**)" in deny
    assert "Edit(~/.gemini/**)" in deny
    forbidden = "".join(["anti", "gravity"])
    assert not any(forbidden in rule for rule in deny)


async def test_claude_client_factory_delegate_none(tmp_path):
    client = await claude_client_factory(tmp_path, resume=None, delegate=None)
    append = client.options.system_prompt["append"]
    assert "External agent" not in append
    settings = json.loads(client.options.settings)
    deny = settings["permissions"]["deny"]
    forbidden = "".join(["anti", "gravity"])
    assert not any(forbidden in rule for rule in deny)


async def test_claude_client_factory_with_delegate(tmp_path):
    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("-p", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=("~/.codex",),
    )
    client = await claude_client_factory(tmp_path, resume=None, delegate=delegate)
    append = client.options.system_prompt["append"]
    assert "## External agent" in append
    assert "`codex -p '<task prompt>'`" in append
    settings = json.loads(client.options.settings)
    deny = settings["permissions"]["deny"]
    assert "Edit(//opt/codex)" in deny
    assert "Edit(~/.codex/**)" in deny


def test_ensure_user_plugin(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # skills does not exist -> None
    assert ensure_user_plugin(state_dir) is None

    # create skills & agents
    claude_dir = home_dir / ".claude"
    skills_dir = claude_dir / "skills"
    agents_dir = claude_dir / "agents"
    skills_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)

    plugin_dir = ensure_user_plugin(state_dir)
    assert plugin_dir is not None
    assert (plugin_dir / ".claude-plugin" / "plugin.json").exists()
    assert (plugin_dir / "skills").is_symlink()
    assert (plugin_dir / "agents").is_symlink()

    # Calling a second time should be idempotent (no error)
    plugin_dir_2 = ensure_user_plugin(state_dir)
    assert plugin_dir_2 == plugin_dir


async def test_plugins_option_reflects_user_plugin(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    skills_dir = home_dir / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_dir))

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    client = await claude_client_factory(tmp_path, resume=None, state_dir=state_dir)
    plugins = client.options.plugins
    assert len(plugins) == 1
    assert plugins[0]["type"] == "local"
    assert plugins[0]["path"] == str(state_dir / "user-plugin")



async def test_mobile_reply_style_is_always_appended(tmp_path):
    """Replies are read on a phone: the brevity rule must reach the model even
    when the project has no CLAUDE.md, and must sit alongside one when it does."""
    from bridge.session import MOBILE_REPLY_STYLE

    bare = (await claude_client_factory(tmp_path, resume=None)).options.system_prompt
    assert bare == {"type": "preset", "preset": "claude_code", "append": MOBILE_REPLY_STYLE}

    (tmp_path / "CLAUDE.md").write_text("project rule", encoding="utf-8")
    with_md = (await claude_client_factory(tmp_path, resume=None)).options.system_prompt
    assert MOBILE_REPLY_STYLE in with_md["append"]
    assert "project rule" in with_md["append"]


def test_mobile_reply_style_contains_sensitive_info_directive():
    from bridge.session import MOBILE_REPLY_STYLE

    assert "Reply in the language the user writes in." in MOBILE_REPLY_STYLE
    assert "conclusion" in MOBILE_REPLY_STYLE.lower()
    assert "secrets" in MOBILE_REPLY_STYLE.lower()



def test_build_deny_rules_linux_persistence_and_absolute_protect_path(tmp_path):
    deny = build_deny_rules(
        tmp_path,
        delegate=Delegate(
            name="codex",
            bin=Path("/opt/codex"),
            args=("{prompt}",),
            timeout_s=60.0,
            protect_paths=("/etc/codex", "~/.codex"),
        ),
    )
    assert "Edit(~/.config/systemd/**)" in deny
    assert "Edit(~/.config/autostart/**)" in deny
    # absolute paths need the "//" form in permission rules
    assert "Edit(//etc/codex/**)" in deny
    assert "Edit(~/.codex/**)" in deny
