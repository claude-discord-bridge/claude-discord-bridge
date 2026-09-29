import asyncio
import json
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from bridge.permissions import AUTO_ALLOW, ApprovalGate


class RecordingPrompter:
    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool_name, tool_input, summary):
        self.calls.append((tool_name, tool_input))
        return self.decisions.pop(0)


class HangingPrompter:
    async def __call__(self, tool_name, tool_input, summary):
        await asyncio.sleep(10)
        return "allow"


def make_gate(tmp_path, prompter, timeout_s=120.0):
    return ApprovalGate(prompter, tmp_path / "audit.log", timeout_s=timeout_s)


async def test_read_is_auto_allowed_without_prompting(tmp_path):
    prompter = RecordingPrompter([])
    gate = make_gate(tmp_path, prompter)
    result = await gate("Read", {"file_path": "/a.py"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert prompter.calls == []


async def test_every_auto_allow_tool_skips_prompt(tmp_path):
    prompter = RecordingPrompter([])
    gate = make_gate(tmp_path, prompter)
    for name in AUTO_ALLOW:
        assert isinstance(await gate(name, {}, None), PermissionResultAllow)
    assert prompter.calls == []


async def test_bash_is_auto_allowed_without_prompt(tmp_path):
    prompter = RecordingPrompter([])
    gate = make_gate(tmp_path, prompter)
    result = await gate("Bash", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is None
    assert prompter.calls == []


async def test_deny_returns_deny_with_message(tmp_path):
    prompter = RecordingPrompter(["deny"])
    gate = make_gate(tmp_path, prompter)
    result = await gate("UnknownDangerousTool", {"command": "rm -rf /"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "거부" in result.message


async def test_unknown_tool_requires_approval(tmp_path):
    prompter = RecordingPrompter(["deny"])
    gate = make_gate(tmp_path, prompter)
    await gate("BrandNewTool", {}, None)
    assert prompter.calls[0][0] == "BrandNewTool"


async def test_timeout_denies(tmp_path):
    gate = make_gate(tmp_path, HangingPrompter(), timeout_s=0.05)
    result = await gate("SomeNewTool", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "시간" in result.message


async def test_allow_all_skips_later_prompts_for_same_tool(tmp_path):
    prompter = RecordingPrompter(["allow_all"])
    gate = make_gate(tmp_path, prompter)
    await gate("SomeNewTool", {"command": "ls"}, None)
    result = await gate("SomeNewTool", {"command": "pwd"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert len(prompter.calls) == 1


async def test_allow_all_does_not_leak_to_other_tools(tmp_path):
    prompter = RecordingPrompter(["allow_all", "deny"])
    gate = make_gate(tmp_path, prompter)
    await gate("SomeNewTool", {"command": "ls"}, None)
    result = await gate("AnotherTool", {"file_path": "/a"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert len(prompter.calls) == 2


async def test_plain_allow_does_not_persist(tmp_path):
    prompter = RecordingPrompter(["allow", "deny"])
    gate = make_gate(tmp_path, prompter)
    await gate("SomeNewTool", {"command": "ls"}, None)
    await gate("SomeNewTool", {"command": "pwd"}, None)
    assert len(prompter.calls) == 2


async def test_decisions_are_audited(tmp_path):
    audit = tmp_path / "audit.log"
    gate = ApprovalGate(RecordingPrompter(["deny"]), audit)
    await gate("SomeNewTool", {"command": "rm -rf node_modules"}, None)
    text = audit.read_text(encoding="utf-8")
    assert "SomeNewTool" in text
    assert "deny" in text
    assert "rm -rf node_modules" in text


async def test_prompter_exception_denies(tmp_path):
    class Boom:
        async def __call__(self, *args):
            raise RuntimeError("discord is down")

    gate = make_gate(tmp_path, Boom())
    result = await gate("SomeNewTool", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultDeny)


# --- Fix round 1: reviewer findings ---------------------------------------


async def test_webfetch_is_blocked_without_prompt(tmp_path):
    prompter = RecordingPrompter([])
    gate = make_gate(tmp_path, prompter)
    result = await gate("WebFetch", {"url": "http://example.com"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert prompter.calls == []


async def test_malformed_decision_values_deny(tmp_path):
    """The gate's central security property: only the exact strings 'allow'
    and 'allow_all' ever grant access. Anything else - a typo, None, or a
    stray object - must resolve to Deny, never escape as Allow."""
    for bad_decision in ("yes", None, object()):
        prompter = RecordingPrompter([bad_decision])
        gate = make_gate(tmp_path, prompter)
        result = await gate("SomeNewTool", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultDeny), repr(bad_decision)


def _read_audit_entries(audit_path: Path) -> list[dict]:
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


async def test_audit_log_records_every_decision_kind(tmp_path):
    audit = tmp_path / "audit.log"

    # allow
    await ApprovalGate(RecordingPrompter(["allow"]), audit)("SomeNewTool1", {"command": "ls"}, None)

    # allow_all on first call, then the cached "allow_all_cached" path on the second
    allow_all_gate = ApprovalGate(RecordingPrompter(["allow_all"]), audit)
    await allow_all_gate("SomeNewTool2", {"command": "ls"}, None)
    await allow_all_gate("SomeNewTool2", {"command": "pwd"}, None)

    # auto_allow (never reaches the prompter at all)
    await ApprovalGate(RecordingPrompter([]), audit)("Read", {"file_path": "/a.py"}, None)

    # timeout
    await ApprovalGate(HangingPrompter(), audit, timeout_s=0.05)("SomeNewTool3", {"command": "ls"}, None)

    # error
    class Boom:
        async def __call__(self, *args):
            raise RuntimeError("boom")

    await ApprovalGate(Boom(), audit)("SomeNewTool4", {"command": "ls"}, None)

    entries = _read_audit_entries(audit)
    decisions = [entry["decision"] for entry in entries]
    for expected in ("allow", "allow_all", "allow_all_cached", "auto_allow", "timeout", "error"):
        assert expected in decisions, decisions

    for entry in entries:
        assert "ts" in entry
        assert "input" in entry


async def test_audit_failure_that_is_not_oserror_does_not_break_decision(tmp_path):
    class Unstringable:
        def __str__(self) -> str:
            raise TypeError("cannot stringify this")

    prompter = RecordingPrompter(["deny"])
    gate = make_gate(tmp_path, prompter)
    # "note" is not one of render._SUMMARY_KEYS, so summarize_tool_use only
    # joins sorted *keys* and never calls str() on this value: the failure is
    # isolated to _audit's own serialization of tool_input, not the summary
    # path, and must not prevent the gate from returning a proper Deny.
    result = await gate("SomeNewTool", {"note": Unstringable()}, None)
    assert isinstance(result, PermissionResultDeny)


def test_auto_allow_set_is_pinned():
    """A change detector, not a restatement: AUTO_ALLOW is the set of tools
    that run on this machine with nobody watching, and BLOCKED is the set that
    never runs. Neither may change by accident -- editing either is a security
    decision, and this assertion forces it to be made deliberately, in a diff
    that someone has to justify. `WebFetch` in particular belongs in BLOCKED:
    it is the one tool whose destination Claude chooses from what it just read.
    """
    from bridge.permissions import BLOCKED
    assert AUTO_ALLOW == {
        "Read", "Grep", "Glob", "TodoWrite", "AskUserQuestion",
        "BashOutput", "KillShell", "KillBash", "Agent", "Task", "Skill",
    }
    assert BLOCKED == {"WebFetch", "WebSearch", "SandboxNetworkAccess"}



# --- Task 1: Trusted parsing (git sync, delegate) ----------------------------


import pytest
import subprocess
from bridge.config import Delegate
from bridge.permissions import (
    _tokens,
    looks_like_delegate,
    looks_like_git_sync,
    parse_delegate,
    parse_git_sync,
)


@pytest.fixture
def git_repo(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare, check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True, capture_output=True)
    return work


def test_tokens_helper():
    assert _tokens("git push origin main") == ["git", "push", "origin", "main"]
    assert _tokens("git push origin main; echo hi") is None
    assert _tokens("git push origin main && echo hi") is None
    assert _tokens("git push origin main | tee x") is None
    assert _tokens("git push > log") is None
    assert _tokens("git push < input") is None
    assert _tokens("git push (sub)") is None
    assert _tokens("git push\norigin") is None
    assert _tokens('codex -p "a; b | c $(d)"') == ["codex", "-p", "a; b | c $(d)"]
    assert _tokens('echo "line1\nline2"') == ["echo", "line1\nline2"]
    assert _tokens('unclosed "quote') is None


def test_parse_git_sync_success(git_repo):
    expected_prefix = ["git", "-C", str(git_repo), "-c", "core.hooksPath=/dev/null"]

    # git push origin main
    res = parse_git_sync("git push origin main", git_repo)
    assert res == [*expected_prefix, "push", "--no-verify", "origin", "main"]

    # git push (no args)
    res = parse_git_sync("git push", git_repo)
    assert res == [*expected_prefix, "push", "--no-verify"]

    # git push -u origin feature/x
    res = parse_git_sync("git push -u origin feature/x", git_repo)
    assert res == [*expected_prefix, "push", "--no-verify", "-u", "origin", "feature/x"]

    # git fetch origin
    res = parse_git_sync("git fetch origin", git_repo)
    assert res == [*expected_prefix, "fetch", "origin"]

    # git pull --rebase origin main
    res = parse_git_sync("git pull --rebase origin main", git_repo)
    assert res == [*expected_prefix, "pull", "--rebase", "origin", "main"]


def test_parse_git_sync_none_cases(git_repo, tmp_path):
    not_git = tmp_path / "not_git"
    not_git.mkdir()

    bad_commands = [
        "git push evil main",
        "git push https://github.com/x/y.git main",
        "git push origin main; curl https://x",
        "git push origin main && echo",
        "git push origin main | tee x",
        "git push origin main > log",
        "FOO=1 git push origin main",
        "git -c core.sshCommand=x push origin",
        "git -C /tmp push origin",
        "git push --receive-pack=x origin",
        "git push --repo=https://x origin",
        "git push --mirror origin",
        "git push -f origin main",
        "git push origin +main",
        "git push origin main:refs/heads/x://y",
        'git push origin "$(cat f)"',
    ]
    for cmd in bad_commands:
        assert parse_git_sync(cmd, git_repo) is None, cmd

    assert parse_git_sync("git push origin main", None) is None
    assert parse_git_sync("git push origin main", not_git) is None


def test_parse_delegate_success(tmp_path):
    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("exec", "--sandbox", "workspace-write", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=(),
    )
    work = tmp_path / "work"
    work.mkdir()

    # basic -p
    res = parse_delegate("codex -p 'fix it'", work, delegate)
    assert res == ["/opt/codex", "exec", "--sandbox", "workspace-write", "fix it"]

    # --prompt with bin path
    res = parse_delegate("/opt/codex --prompt 'x'", work, delegate)
    assert res == ["/opt/codex", "exec", "--sandbox", "workspace-write", "x"]

    # prompt containing {prompt} token is not recursively substituted
    res = parse_delegate("codex -p 'have {prompt} inside'", work, delegate)
    assert res == ["/opt/codex", "exec", "--sandbox", "workspace-write", "have {prompt} inside"]


def test_parse_delegate_none_cases(tmp_path):
    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("exec", "--sandbox", "workspace-write", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=(),
    )
    work = tmp_path / "work"
    work.mkdir()

    bad_commands = [
        "codex -p a --yolo",
        "codex --yolo -p a",
        "codex exec x",
        "codex -p a | tee f",
        "codex -p a; rm x",
        "echo codex -p a",
        "codex",
        "codex -p",
        "codex -p a b",
        "/tmp/other -p a",
    ]
    for cmd in bad_commands:
        assert parse_delegate(cmd, work, delegate) is None, cmd

    assert parse_delegate("codex -p x", None, delegate) is None
    assert parse_delegate("codex -p x", work, None) is None


def test_looks_like():
    assert looks_like_git_sync("cd x && git push origin") is True
    assert looks_like_git_sync("git status") is False
    assert looks_like_git_sync("echo push") is False

    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("exec", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=(),
    )
    assert looks_like_delegate("PLAN=x; codex -p y", delegate) is True
    assert looks_like_delegate("codex -p a | tee f", delegate) is True
    assert looks_like_delegate("echo codexing", delegate) is False
    assert looks_like_delegate("PLAN=x; codex -p y", None) is False


# --- Task 2: Gate overhaul & trusted runner ---------------------------------


from bridge.permissions import (
    BLOCKED,
    PATH_TOOLS,
    RESULT_TAIL_CHARS,
    is_publish_command,
    run_trusted,
)


def test_is_publish_command():
    blocked = [
        "npm publish",
        "cd pkg && npm publish --access public",
        "./gradlew publishToMavenCentral",
        "mvn -B deploy",
        "twine upload dist/*",
        "cargo publish",
        "docker push x/y",
        "bun publish",
        "pnpm unpublish",
        "yarn npm publish",
        "poetry publish",
        "gem push gem-0.1.gem",
    ]
    for cmd in blocked:
        assert is_publish_command(cmd) is True, cmd

    allowed = [
        "npm install",
        "npm run build",
        "./gradlew build",
        "mvn package",
        "pip install requests",
        "echo publish",
    ]
    for cmd in allowed:
        assert is_publish_command(cmd) is False, cmd


async def test_run_trusted_basic(tmp_path):
    out_file, rc = await run_trusted(["python3", "-c", "print('hi')"], tmp_path, 5.0)
    assert rc == 0
    assert Path(out_file).read_text(encoding="utf-8") == "hi\n"


async def test_run_trusted_timeout(tmp_path):
    out_file, rc = await run_trusted(["python3", "-c", "import time; time.sleep(5)"], tmp_path, 0.5)
    assert rc == 124
    content = Path(out_file).read_text(encoding="utf-8")
    assert "[⏱ 0.5s 초과로 중단]" in content


async def test_run_trusted_missing_binary(tmp_path):
    out_file, rc = await run_trusted(["nonexistent_binary_xyz_123"], tmp_path, 5.0)
    assert rc == 127
    content = Path(out_file).read_text(encoding="utf-8")
    assert "[실행 실패]" in content


async def test_run_trusted_tail_truncate(tmp_path):
    out_file, rc = await run_trusted(["python3", "-c", "print('A' * 40000)"], tmp_path, 5.0)
    assert rc == 0
    content = Path(out_file).read_text(encoding="utf-8")
    assert len(content) <= RESULT_TAIL_CHARS + 30
    assert content.startswith("[…앞부분 생략…]\n")


async def test_run_trusted_cwd(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    out_file, rc = await run_trusted(["python3", "-c", "import os; print(os.getcwd())"], sub, 5.0)
    assert rc == 0
    content = Path(out_file).read_text(encoding="utf-8").strip()
    assert content == str(sub.resolve())


async def test_gate_bash_dangerously_disable_sandbox_denied(tmp_path):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    res = await gate("Bash", {"command": "ls", "dangerouslyDisableSandbox": True}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "샌드박스 해제 요청 차단" in res.message
    assert len(blocked_calls) == 1
    assert "샌드박스 해제 요청 차단" in blocked_calls[0][1]


async def test_gate_bash_git_push_trusted(tmp_path, git_repo, monkeypatch):
    recorded = []
    fake_out = tmp_path / "fake_out.txt"
    fake_out.write_text("pushed successfully\n")

    async def fake_run_trusted(argv, cwd, timeout_s):
        recorded.append((argv, cwd, timeout_s))
        return str(fake_out), 0

    monkeypatch.setattr("bridge.permissions.run_trusted", fake_run_trusted)

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log")
    gate.cwd = git_repo

    res = await gate("Bash", {"command": "git push origin main"}, None)
    assert isinstance(res, PermissionResultAllow)
    assert res.updated_input is not None
    assert res.updated_input["command"] == f"cat {fake_out}; exit 0"
    assert "dangerouslyDisableSandbox" not in res.updated_input
    assert len(recorded) == 1
    argv, cwd, timeout = recorded[0]
    assert cwd == git_repo
    assert argv == ["git", "-C", str(git_repo), "-c", "core.hooksPath=/dev/null", "push", "--no-verify", "origin", "main"]


async def test_gate_bash_git_push_bad_blocked(tmp_path, git_repo):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    gate.cwd = git_repo

    res = await gate("Bash", {"command": "git push https://github.com/x/y.git"}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "git push/fetch/pull은 단독 명령으로" in res.message
    assert len(blocked_calls) == 1


async def test_gate_bash_delegate_trusted_and_not_blocked_by_publish(tmp_path, monkeypatch):
    recorded = []
    fake_out = tmp_path / "fake_delegate_out.txt"
    fake_out.write_text("OK\n")

    async def fake_run_trusted(argv, cwd, timeout_s):
        recorded.append((argv, cwd, timeout_s))
        return str(fake_out), 0

    monkeypatch.setattr("bridge.permissions.run_trusted", fake_run_trusted)

    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("exec", "--sandbox", "workspace-write", "{prompt}"),
        timeout_s=1234.0,
        protect_paths=(),
    )
    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", delegate=delegate)
    gate.cwd = tmp_path

    cmd = "codex -p 'npm publish 관련 문서 작성'"
    res = await gate("Bash", {"command": cmd}, None)
    assert isinstance(res, PermissionResultAllow)
    assert res.updated_input is not None
    assert res.updated_input["command"] == f"cat {fake_out}; exit 0"
    assert len(recorded) == 1
    argv, cwd, timeout = recorded[0]
    assert cwd == tmp_path
    assert timeout == 1234.0
    assert argv == ["/opt/codex", "exec", "--sandbox", "workspace-write", "npm publish 관련 문서 작성"]


async def test_gate_bash_delegate_pipe_blocked(tmp_path):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("-p", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=(),
    )
    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block, delegate=delegate)
    gate.cwd = tmp_path

    res = await gate("Bash", {"command": "codex -p x | tee log"}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "codex는 단독 명령으로만: codex -p '<프롬프트>'" in res.message
    assert len(blocked_calls) == 1


async def test_gate_bash_delegate_none_is_sandboxed(tmp_path):
    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", delegate=None)
    gate.cwd = tmp_path

    res = await gate("Bash", {"command": "codex -p hi"}, None)
    assert isinstance(res, PermissionResultAllow)
    assert res.updated_input is None


async def test_gate_bash_publish_commands_blocked(tmp_path):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    gate.cwd = tmp_path

    publish_cmds = [
        "npm publish",
        "cd pkg && npm publish --access public",
        "./gradlew publishToMavenCentral",
        "mvn -B deploy",
        "twine upload dist/*",
        "cargo publish",
        "docker push x/y",
    ]
    for cmd in publish_cmds:
        res = await gate("Bash", {"command": cmd}, None)
        assert isinstance(res, PermissionResultDeny)
        assert "패키지 배포 명령 차단" in res.message

    assert len(blocked_calls) == len(publish_cmds)


async def test_gate_bash_allowed_commands(tmp_path):
    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log")
    gate.cwd = tmp_path

    allowed = [
        "npm install",
        "npm run build",
        "./gradlew build",
        "mvn package",
        "pip install requests",
        "echo publish",
    ]
    for cmd in allowed:
        res = await gate("Bash", {"command": cmd}, None)
        assert isinstance(res, PermissionResultAllow)


async def test_gate_sandbox_network_access_blocked(tmp_path):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    res = await gate("SandboxNetworkAccess", {"host": "example.com"}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "외부 접속 차단: example.com" in res.message
    assert len(blocked_calls) == 1
    assert "example.com" in blocked_calls[0][1]


async def test_gate_websearch_and_mcp_blocked(tmp_path):
    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    for tool_name in ("WebSearch", "mcp__foo__bar"):
        res = await gate(tool_name, {}, None)
        assert isinstance(res, PermissionResultDeny)

    assert len(blocked_calls) == 2


async def test_gate_agent_task_skill_auto_allowed(tmp_path):
    prompter = RecordingPrompter([])
    gate = ApprovalGate(prompter, tmp_path / "audit.log")
    for tool_name in ("Agent", "Task", "Skill"):
        res = await gate(tool_name, {}, None)
        assert isinstance(res, PermissionResultAllow)
    assert prompter.calls == []


async def test_gate_write_path_tools(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    symlink_outside = work / "link_to_outside"
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    symlink_outside.symlink_to(outside_dir)

    blocked_calls = []
    async def on_block(summary, reason):
        blocked_calls.append((summary, reason))

    gate = ApprovalGate(RecordingPrompter([]), tmp_path / "audit.log", on_block=on_block)
    gate.cwd = work

    # inside cwd -> allow
    res = await gate("Write", {"file_path": str(work / "a.txt")}, None)
    assert isinstance(res, PermissionResultAllow)

    # relative inside -> allow
    res = await gate("Write", {"file_path": "nested/b.txt"}, None)
    assert isinstance(res, PermissionResultAllow)

    # outside -> deny
    bad_paths = [
        str(work / "../x.txt"),
        "/etc/x",
        str(symlink_outside / "evil.txt"),
    ]
    for p in bad_paths:
        res = await gate("Write", {"file_path": p}, None)
        assert isinstance(res, PermissionResultDeny)
        assert "작업 디렉터리 밖 쓰기 차단" in res.message

    # gate.cwd is None -> deny
    gate.cwd = None
    res = await gate("Write", {"file_path": "a.txt"}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "작업 디렉터리 밖 쓰기 차단" in res.message


async def test_gate_audit_and_on_block_error_handling(tmp_path):
    async def faulty_on_block(summary, reason):
        raise RuntimeError("network down")

    audit = tmp_path / "audit.log"
    gate = ApprovalGate(RecordingPrompter([]), audit, on_block=faulty_on_block)
    res = await gate("SandboxNetworkAccess", {"host": "example.com"}, None)
    assert isinstance(res, PermissionResultDeny)

    entries = _read_audit_entries(audit)
    decisions = [e["decision"] for e in entries]
    assert "blocked" in decisions




def test_parse_delegate_rejects_flag_injection(tmp_path):
    # A prompt landing in the {prompt} slot must never be read as an agent flag.
    delegate = Delegate(
        name="codex",
        bin=Path("/opt/codex"),
        args=("exec", "--sandbox", "workspace-write", "{prompt}"),
        timeout_s=3000.0,
        protect_paths=(),
    )
    work = tmp_path / "work"
    work.mkdir()
    assert parse_delegate("codex -p '--dangerously-bypass-approvals-and-sandbox'", work, delegate) is None
    assert parse_delegate("codex -p -x", work, delegate) is None
    assert parse_delegate("codex -p ''", work, delegate) is None
    assert parse_delegate("codex -p '  '", work, delegate) is None
    # a leading space is fine: it is not a flag
    assert parse_delegate("codex -p ' do -x'", work, delegate)[-1] == " do -x"
