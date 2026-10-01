"""소스가 돌려주는 글 한 건의 공통 형태."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Entry:
    title: str
    link: str
    published: Optional[datetime] = None
    summary: str = ""
    uid: str = field(default="")
    badge: str = ""                                   # 소스명 뒤에 붙는 표시 (예: "🔄 전개")
    detail_lines: list[str] = field(default_factory=list)  # 요약 대신 보여줄 줄들
    dedup_by_title: bool = True                       # 같은 제목이 반복돼야 하는 경우 False

    def __post_init__(self) -> None:
        # 피드가 guid를 안 주면 링크로, 링크도 없으면 제목으로 식별한다.
        raw = self.uid or self.link or self.title
        self.uid = hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:16]
        if self.published and self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=timezone.utc)

    @property
    def dedup_keys(self) -> tuple[str, ...]:
        """링크와 제목, 두 가지로 중복을 판정한다.

        구글 뉴스는 같은 기사를 언론사별 다른 URL로 주기 때문에
        링크만 보면 같은 기사가 여러 번 온다.
        """
        if not self.dedup_by_title:
            # 이슈 전개 알림은 제목이 그대로인 채 내용만 달라진다.
            return (self.uid,)
        normalized = re.sub(r"\W", "", self.title.lower())
        title_key = "t:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        return self.uid, title_key

    @property
    def haystack(self) -> str:
        """키워드를 찾을 대상 텍스트."""
        return f"{self.title}\n{self.summary}"
