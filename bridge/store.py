"""Discord 쓰레드와 Claude 세션 ID의 매핑을 디스크에 보존한다."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadRecord:
    session_id: str | None
    cwd: Path


class ThreadStore:
    """threads.json을 감싼 얇은 저장소. 쓰기는 원자적이다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[int, ThreadRecord] = self._load()

    def _load(self) -> dict[int, ThreadRecord]:
        try:
            if not self._path.exists():
                return {}
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                int(key): ThreadRecord(
                    session_id=value.get("session_id"),
                    cwd=Path(value["cwd"]),
                )
                for key, value in raw.items()
            }
        except Exception:
            # threads.json은 신뢰할 수 없는 입력이다. 어떤 방식으로 잘못됐든
            # (문법 오류, 배열 최상위, 항목이 dict가 아님, cwd가 문자열이
            # 아님, exists() 자체가 권한 오류 등으로 예외를 던짐 등) launchd
            # KeepAlive 재시작 루프를 만들지 않도록 넓게 잡아 빈 저장소로
            # 시작한다. 파일이 그냥 없는 정상적인 최초 실행 경우는 위의
            # `return {}`로 조용히 처리되며 이 경고는 남기지 않는다.
            # KeyboardInterrupt/SystemExit는 Exception의 하위 클래스가
            # 아니므로 그대로 전파된다.
            logger.warning("threads.json unreadable; starting empty", exc_info=True)
            return {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            str(thread_id): {
                "session_id": record.session_id,
                "cwd": str(record.cwd),
            }
            for thread_id, record in self._cache.items()
        }
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def get(self, thread_id: int) -> ThreadRecord | None:
        return self._cache.get(thread_id)

    def all(self) -> dict[int, ThreadRecord]:
        return dict(self._cache)

    def put(self, thread_id: int, session_id: str | None, cwd: Path) -> None:
        self._cache[thread_id] = ThreadRecord(session_id=session_id, cwd=cwd)
        self._flush()

    def delete(self, thread_id: int) -> None:
        self._cache.pop(thread_id, None)
        self._flush()
