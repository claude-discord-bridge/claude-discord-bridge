from bridge.render import DISCORD_LIMIT, split_for_discord


def test_short_text_is_one_chunk():
    assert split_for_discord("hello") == ["hello"]


def test_long_text_splits_under_limit():
    text = "\n".join(f"line {i}" for i in range(1000))
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)


def test_no_content_is_lost():
    text = "\n".join(f"line {i}" for i in range(1000))
    joined = "\n".join(split_for_discord(text))
    assert "line 0" in joined
    assert "line 999" in joined


def test_code_fence_is_balanced_in_every_chunk():
    body = "\n".join(f"    x = {i}" for i in range(600))
    text = f"앞말\n\n```python\n{body}\n```\n\n뒷말"
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:200]


def test_reopened_fence_keeps_language():
    body = "\n".join(f"    x = {i}" for i in range(600))
    text = f"```python\n{body}\n```"
    chunks = split_for_discord(text)
    assert chunks[1].startswith("```python")


def test_single_line_longer_than_limit_is_hard_wrapped():
    text = "x" * 5000
    chunks = split_for_discord(text)
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "".join(chunks).count("x") == 5000


def test_oversized_fence_language_is_clamped():
    long_lang = "w" * 5000
    body = "\n".join(f"    x = {i}" for i in range(600))
    text = f"```{long_lang}\n{body}\n```"
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:200]


def test_unclosed_fence_at_eof():
    body = "\n".join(f"    x = {i}" for i in range(600))
    text = f"start\n\n```python\n{body}"
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:200]


def test_fence_as_last_line():
    body = "\n".join(f"    line {i}" for i in range(300))
    text = f"```python\n{body}\n```"
    chunks = split_for_discord(text)
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:200]
    assert not chunks[-1].endswith("```\n```"), "Tail must not double-close fence"


def test_javascript_tag_survives_split():
    body = "\n".join(f"    const x = {i};" for i in range(600))
    text = f"```javascript\n{body}\n```"
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    if len(chunks) > 1:
        assert chunks[1].startswith("```javascript"), "Reopened chunk must preserve full language tag"
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:200]


from claude_agent_sdk import ResultMessage

from bridge.render import (
    format_footer,
    format_heartbeat,
    summarize_tool_use,
)


def test_bash_summary_shows_command():
    assert summarize_tool_use("Bash", {"command": "npm test"}) == "🔧 Bash: `npm test`"


def test_edit_summary_shows_path():
    out = summarize_tool_use("Edit", {"file_path": "/a/b/c.py"})
    assert out == "🔧 Edit: `/a/b/c.py`"


def test_read_summary_shows_path():
    out = summarize_tool_use("Read", {"file_path": "/a/b/c.py"})
    assert out == "🔧 Read: `/a/b/c.py`"


def test_unknown_tool_falls_back_to_key_list():
    out = summarize_tool_use("Mystery", {"alpha": 1, "beta": 2})
    assert out.startswith("🔧 Mystery:")
    assert "alpha" in out


def test_long_command_is_truncated():
    out = summarize_tool_use("Bash", {"command": "x" * 500})
    assert len(out) <= 220


def test_footer_shows_cost_and_turns():
    result = ResultMessage(
        subtype="success",
        duration_ms=4200,
        duration_api_ms=4000,
        is_error=False,
        num_turns=3,
        session_id="abc-123",
        total_cost_usd=0.0123,
    )
    footer = format_footer(result)
    assert "$0.0123" in footer
    assert "3" in footer
    assert "4.2s" in footer


def test_footer_handles_missing_cost():
    result = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="abc-123",
        total_cost_usd=None,
    )
    assert "$" not in format_footer(result)


def test_heartbeat_text():
    assert format_heartbeat(125.0, 14) == "⏳ 작업 중… (2분 5초 경과, 툴 14회)"


def test_very_long_mcp_tool_name_stays_bounded():
    name = "mcp__" + "x" * 250 + "__tool"  # 261 chars, MCP-shaped
    assert len(name) == 261
    out = summarize_tool_use(name, {"command": "npm test"})
    assert len(out) <= 220


def test_long_name_boundary_sweep_keeps_backticks_balanced():
    for length in range(200, 261):
        name = "m" * length
        out = summarize_tool_use(name, {"command": "npm test"})
        assert out.count("`") % 2 == 0, (length, out)
        assert len(out) <= 220, (length, out)


from bridge.render import redact_sensitive


def test_redact_passwords_and_keys():
    assert redact_sensitive("password=hunter2") == "password=[가림]"
    assert redact_sensitive('"api_key": "abc123"') == '"api_key": "[가림]"'
    assert redact_sensitive("DB_PASSWORD: s3cr3t") == "DB_PASSWORD: [가림]"
    assert redact_sensitive("MYSQL_ROOT_PASSWORD=x") == "MYSQL_ROOT_PASSWORD=[가림]"
    assert redact_sensitive("비밀번호: 1234abcd") == "비밀번호: [가림]"
    assert redact_sensitive("암호 = testpass") == "암호 = [가림]"


def test_redact_url_credentials():
    assert (
        redact_sensitive("jdbc:mysql://admin:pw@db.internal:3306/x")
        == "jdbc:mysql://admin:[가림]@db.internal:3306/x"
    )
    assert (
        redact_sensitive("https://user:tok@github.com/a/b.git")
        == "https://user:[가림]@github.com/a/b.git"
    )
    assert (
        redact_sensitive("postgres://u:pw@10.1.1.1/db")
        == "postgres://u:[가림]@[IP 가림]/db"
    )


def test_redact_tokens_and_keys():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y1...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert redact_sensitive(pem) == "[개인키 가림]"

    ghp = "ghp_" + "a" * 36
    assert redact_sensitive(f"token is {ghp}") == "token is [토큰 가림]"

    akia = "AKIA" + "1234567890ABCDEF"
    assert redact_sensitive(f"key={akia}") == "key=[토큰 가림]"

    sk_ant = "sk-ant-" + "a" * 30
    assert redact_sensitive(sk_ant) == "[토큰 가림]"

    sk = "sk-" + "b" * 25
    assert redact_sensitive(sk) == "[토큰 가림]"

    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    assert redact_sensitive(jwt) == "[토큰 가림]"

    disco = "MTAwMDAwMDAwMDAwMDAwMDAwMA.G12345.abcdefghijklmnopqrstuvwxyz1234567"
    assert redact_sensitive(disco) == "[토큰 가림]"

    assert (
        redact_sensitive("Authorization: Bearer abcdefgh12345")
        == "Authorization: Bearer [가림]"
    )
    assert redact_sensitive("basic dXNlcjpwYXNz") == "basic [가림]"


def test_redact_personal_info():
    assert redact_sensitive("900101-1234567") == "[주민번호 가림]"
    assert redact_sensitive("9001011234567") == "[주민번호 가림]"

    assert redact_sensitive("4111 1111 1111 1111") == "[카드번호 가림]"
    assert redact_sensitive("4111-1111-1111-1111") == "[카드번호 가림]"
    assert redact_sensitive("1234 5678 9012 3456") == "1234 5678 9012 3456"

    assert redact_sensitive("010-1234-5678") == "[전화번호 가림]"
    assert redact_sensitive("01012345678") == "[전화번호 가림]"
    assert redact_sensitive("02-123-4567") == "[전화번호 가림]"

    assert redact_sensitive("a.b@example.com") == "[이메일 가림]"


def test_redact_ipv4():
    assert redact_sensitive("서버 10.0.3.15 에 배포") == "서버 [IP 가림] 에 배포"
    assert redact_sensitive("192.168.0.1:8080") == "[IP 가림]:8080"
    assert redact_sensitive("127.0.0.1") == "127.0.0.1"
    assert redact_sensitive("0.0.0.0") == "0.0.0.0"
    assert redact_sensitive("version 2.1.280") == "version 2.1.280"
    assert redact_sensitive("3/4 tests passed") == "3/4 tests passed"


def test_do_not_redact_false_positives():
    assert redact_sensitive("timeout=30") == "timeout=30"
    assert redact_sensitive("passed") == "passed"
    assert redact_sensitive("bypass") == "bypass"
    assert redact_sensitive("compass") == "compass"
    assert (
        redact_sensitive("password 정책을 바꿨습니다")
        == "password 정책을 바꿨습니다"
    )
    assert redact_sensitive("bridge/permissions.py") == "bridge/permissions.py"
    assert redact_sensitive("일반적인 한국어 문장입니다.") == "일반적인 한국어 문장입니다."


def test_idempotency():
    samples = [
        "password=hunter2",
        '"api_key": "abc123"',
        "DB_PASSWORD: s3cr3t",
        "비밀번호: 1234abcd",
        "jdbc:mysql://admin:pw@db.internal:3306/x",
        "https://user:tok@github.com/a/b.git",
        "ghp_" + "a" * 36,
        "AKIA1234567890ABCDEF",
        "Authorization: Bearer abcdefgh12345",
        "900101-1234567",
        "4111 1111 1111 1111",
        "010-1234-5678",
        "a.b@example.com",
        "서버 10.0.3.15 에 배포",
        "192.168.0.1:8080",
        "127.0.0.1",
        "timeout=30",
    ]
    for s in samples:
        once = redact_sensitive(s)
        twice = redact_sensitive(once)
        assert once == twice, f"Not idempotent for {s!r}: {once!r} != {twice!r}"


def test_url_credential_and_email_interaction():
    text = "https://user:pw@example.com/repo"
    redacted = redact_sensitive(text)
    assert redacted == "https://user:[가림]@example.com/repo"
    assert redact_sensitive(redacted) == redacted

