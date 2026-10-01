"""이미 보낸 글 기록. 저장소에 커밋되어 실행 간에 유지된다."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RETENTION_DAYS = 90  # 이보다 오래된 기록은 지워서 파일이 무한정 커지지 않게 한다.


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {"sources": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("sources"), dict):
                    self._data = loaded
                else:
                    log.warning("상태 파일 형식이 이상해서 새로 시작합니다: %s", self.path)
            except json.JSONDecodeError:
                # 기록이 깨져도 알림은 계속 돌아야 한다. 최악의 경우 재시딩된다.
                log.warning("상태 파일을 읽을 수 없어 새로 시작합니다: %s", self.path)

    def _bucket(self, source_key: str) -> dict[str, Any]:
        return self._data["sources"].setdefault(source_key, {"initialized": False, "seen": {}, "meta": {}})

    def is_initialized(self, source_key: str) -> bool:
        return bool(self._bucket(source_key).get("initialized"))

    def mark_initialized(self, source_key: str) -> None:
        self._bucket(source_key)["initialized"] = True

    def has_seen(self, source_key: str, uid: str) -> bool:
        return uid in self._bucket(source_key)["seen"]

    def mark_seen(self, source_key: str, uid: str) -> None:
        self._bucket(source_key)["seen"][uid] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get_meta(self, source_key: str, field: str) -> Any:
        return self._bucket(source_key).setdefault("meta", {}).get(field)

    def set_meta(self, source_key: str, field: str, value: Any) -> None:
        self._bucket(source_key).setdefault("meta", {})[field] = value

    def prune(self, active_keys: set[str]) -> None:
        """설정에서 빠진 소스와 오래된 기록을 정리한다."""
        for key in list(self._data["sources"]):
            if key not in active_keys:
                del self._data["sources"][key]
                log.info("설정에 없는 소스 기록을 정리했습니다: %s", key)

        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for bucket in self._data["sources"].values():
            bucket["seen"] = {
                uid: ts
                for uid, ts in bucket["seen"].items()
                if _parse(ts) is None or _parse(ts) > cutoff  # 날짜를 못 읽으면 보수적으로 남겨둔다
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 키를 정렬해야 매 실행마다 의미 없는 diff가 생기지 않는다.
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.write_text(payload + "\n", encoding="utf-8")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
