"""SDK 메시지 스트림을 Discord가 받아들이는 형태로 바꾼다. 순수 함수만."""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import ResultMessage

DISCORD_LIMIT = 2000
ATTACHMENT_THRESHOLD = 6000

_FENCE_RE = re.compile(r"^```(\w*)\s*$")

# 1. PEM 개인키 블록
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# 2. URL 속 자격증명 (jdbc:mysql:// 등의 서브스킴 콜론도 지원)
_URL_CREDENTIALS_RE = re.compile(
    r"\b([a-z][a-z0-9+.\-:]*://)([^\s:/@]+):([^\s@/]+)@"
)

# 3. 알려진 토큰 형식
_KNOWN_TOKENS_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:"
    r"AKIA[0-9A-Z]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{36,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"sk-[A-Za-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_\-]{35}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|"
    r"[MN][A-Za-z\d]{23,}\.[\w\-]{6}\.[\w\-]{27,}"
    r")(?![A-Za-z0-9_\-])"
)

# 4. Bearer/Basic 헤더
_AUTH_HEADER_RE = re.compile(
    r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}",
    re.IGNORECASE,
)

# 5. 키=값 형태 비밀
_KV_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(password|passwd|pwd|pass|secret|token|api[_\-]?key|apikey|access[_\-]?key|secret[_\-]?key|private[_\-]?key|client[_\-]?secret|auth[_\-]?token|credentials?)"
    r"(?![A-Za-z0-9])"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"(\[[^\]]+\]|[^\s\"',;]+)",
    re.IGNORECASE,
)

# 6. 한국어 키=값
_KOREAN_KV_RE = re.compile(
    r"(비밀번호|비번|암호|패스워드)(\s*[:=：]\s*)(\[[^\]]+\]|\S+)"
)

# 7. 주민등록번호
_RESIDENT_ID_RE = re.compile(r"\b\d{6}-?[1-4]\d{6}\b")

# 8. 카드번호 (Luhn 체크)
_CARD_NUMBER_RE = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")

# 9. 전화번호 (휴대폰, 유선)
_PHONE_NUMBER_RE = re.compile(
    r"\b(?:01[016789]-?\d{3,4}-?\d{4}|0\d{1,2}-\d{3,4}-\d{4})\b"
)

# 10. 이메일
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# 11. IPv4 (네 자리 버전 문자열 예: 1.2.3.4 도 가릴 수 있음 — 계획서에 따라 허용)
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)


def _luhn_check(num_str: str) -> bool:
    digits = [int(c) for c in num_str if c.isdigit()]
    if len(digits) != 16:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            doubled = d * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += d
    return checksum % 10 == 0


def _redact_card(match: re.Match) -> str:
    raw = match.group(0)
    if _luhn_check(raw):
        return "[카드번호 가림]"
    return raw


def _redact_ipv4(match: re.Match) -> str:
    ip = match.group(0)
    if ip.startswith("127.") or ip == "0.0.0.0":
        return ip
    return "[IP 가림]"


def _redact_kv(match: re.Match) -> str:
    key = match.group(1)
    delim = match.group(2)
    val = match.group(3)
    if val == "[가림]" or (val.startswith("[") and val.endswith("]") and "가림" in val):
        return match.group(0)
    return f"{key}{delim}[가림]"


def _redact_korean_kv(match: re.Match) -> str:
    key = match.group(1)
    delim = match.group(2)
    val = match.group(3)
    if val == "[가림]" or (val.startswith("[") and val.endswith("]") and "가림" in val):
        return match.group(0)
    return f"{key}{delim}[가림]"


def redact_sensitive(text: str) -> str:
    """텍스트 내 비밀번호, 토큰, IP, 개인정보 등을 가린다. 순수 함수이며 멱등적이다."""
    if not text:
        return text

    # 1. PEM 개인키
    text = _PEM_PRIVATE_KEY_RE.sub("[개인키 가림]", text)
    # 2. URL 속 자격증명
    text = _URL_CREDENTIALS_RE.sub(r"\1\2:[가림]@", text)
    # 3. 알려진 토큰 형식
    text = _KNOWN_TOKENS_RE.sub("[토큰 가림]", text)
    # 4. Bearer/Basic 헤더
    text = _AUTH_HEADER_RE.sub(r"\1 [가림]", text)
    # 5. 키=값 형태 비밀
    text = _KV_SECRET_RE.sub(_redact_kv, text)
    # 6. 한국어 키=값
    text = _KOREAN_KV_RE.sub(_redact_korean_kv, text)
    # 7. 주민등록번호
    text = _RESIDENT_ID_RE.sub("[주민번호 가림]", text)
    # 8. 카드번호 (Luhn 체크)
    text = _CARD_NUMBER_RE.sub(_redact_card, text)
    # 9. 전화번호
    text = _PHONE_NUMBER_RE.sub("[전화번호 가림]", text)
    # 10. 이메일
    text = _EMAIL_RE.sub("[이메일 가림]", text)
    # 11. IPv4 (네 자리 버전 문자열 등도 가릴 수 있음 — 허용)
    text = _IPV4_RE.sub(_redact_ipv4, text)

    return text



def _hard_wrap(line: str, width: int) -> list[str]:
    """한 줄이 한도보다 길면 잘라서 여러 줄로 만든다."""
    if len(line) <= width:
        return [line]
    return [line[i : i + width] for i in range(0, len(line), width)]


def split_for_discord(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """코드 펜스를 깨지 않고 limit 이하 조각으로 나눈다."""
    if len(text) <= limit:
        return [text]

    # 펜스 재개행("```python" + 닫는 펜스)에 쓸 여유를 남긴다.
    # 최대 16자 언어로 클램프되므로 더 큰 여유 필요
    width = limit - 24
    lines: list[str] = []
    for raw in text.split("\n"):
        lines.extend(_hard_wrap(raw, width))

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    fence_lang: str | None = None  # None이면 펜스 바깥
    reopen: str | None = None

    def start_buf() -> None:
        nonlocal buf, size
        buf = []
        size = 0
        if reopen is not None:
            header = "```" + reopen
            buf.append(header)
            size = len(header) + 1

    def flush() -> None:
        nonlocal reopen
        if not buf:
            return
        body = "\n".join(buf)
        if fence_lang is not None:
            body += "\n```"
            reopen = fence_lang
        else:
            reopen = None
        chunks.append(body)
        start_buf()

    start_buf()
    for line in lines:
        if buf and size + len(line) + 1 > width:
            flush()
        buf.append(line)
        size += len(line) + 1
        match = _FENCE_RE.match(line)
        if match:
            fence_lang = match.group(1)[:16] if fence_lang is None else None

    tail = "\n".join(buf)
    if tail.strip():
        if fence_lang is not None:
            tail += "\n```"
        chunks.append(tail)

    return chunks


_SUMMARY_KEYS = ("command", "file_path", "pattern", "path", "url", "description")
_SUMMARY_LINE_CAP = 220


def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> str:
    """도구 호출을 한 줄로 줄인다."""
    detail = ""
    for key in _SUMMARY_KEYS:
        if key in tool_input:
            detail = str(tool_input[key])
            break
    if not detail:
        detail = ", ".join(sorted(tool_input)) or "(no input)"

    detail = detail.replace("\n", " ⏎ ")
    budget = max(0, 200 - len(name))
    if len(detail) > budget:
        keep = max(budget - 1, 0)
        detail = detail[:keep] + "…"

    # 조립된 문자열을 자르지 않는다: 백틱 쌍이 항상 온전하도록, 이름 표시분의
    # 예산을 먼저 정하고 필요하면 이름을 줄인 뒤 마지막에 조립한다.
    fixed_overhead = len("🔧 ") + len(": `") + len("`")
    max_name_len = max(0, _SUMMARY_LINE_CAP - fixed_overhead - len(detail))
    display_name = name
    if len(display_name) > max_name_len:
        keep_name = max(max_name_len - 1, 0)
        display_name = display_name[:keep_name] + "…"
    return f"🔧 {display_name}: `{detail}`"


def format_footer(result: ResultMessage) -> str:
    """최종 결과 메시지 아래에 붙일 한 줄."""
    parts = [f"{result.duration_ms / 1000:.1f}s", f"턴 {result.num_turns}"]
    if result.total_cost_usd is not None:
        parts.append(f"${result.total_cost_usd:.4f}")
    return "— " + " · ".join(parts)


def format_heartbeat(elapsed_s: float, tool_count: int) -> str:
    """장시간 작업 중 갱신할 진행 표시."""
    minutes, seconds = divmod(int(elapsed_s), 60)
    if minutes:
        elapsed = f"{minutes}분 {seconds}초"
    else:
        elapsed = f"{seconds}초"
    return f"⏳ 작업 중… ({elapsed} 경과, 툴 {tool_count}회)"
