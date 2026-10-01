"""RSS/Atom 피드를 Entry 목록으로 바꾼다."""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone

import feedparser

from ..http import get
from ..models import Entry

_TAGS = re.compile(r"<[^>]+>")

# 많은 피드가 요약 끝에 매체 홍보 문구를 붙인다 (워드프레스 기본 동작).
#   "게시물 ○○○ 이 코인데스크 코리아 에 처음 등장했습니다."
#   "The post ○○○ appeared first on ○○○."
# 이 문구에 매체명이 들어있어서, 매체명이 키워드와 겹치면(예: 키워드 '코인' +
# 매체 '코인데스크') 그 피드의 **모든 글**이 필터를 통과해 버린다. 실측으로 겪었다.
_BOILERPLATE = (
    re.compile(r"(?:이\s*)?게시물\s+.*?(?:이|은|는)\s+.*?에\s+처음\s+(?:등장했|게시되었|나타났)\S*\.?\s*$"),
    re.compile(r"The post\s+.*?appeared first on\s+.*?\.?\s*$", re.IGNORECASE),
)


def parse(session, url: str) -> list[Entry]:
    # feedparser 가 직접 받게 두면 UA/타임아웃/재시도를 못 쓰므로 세션으로 받아 넘긴다.
    response = get(session, url)
    return from_bytes(response.content)


def from_bytes(payload: bytes) -> list[Entry]:
    parsed = feedparser.parse(payload)
    return [_to_entry(item) for item in parsed.entries if item.get("link")]


def _to_entry(item) -> Entry:
    summary = item.get("summary") or ""
    if not summary and item.get("content"):
        summary = item["content"][0].get("value", "")
    # 유튜브 피드는 media:group 안에 설명을 넣는다.
    if not summary and item.get("media_description"):
        summary = item["media_description"]

    return Entry(
        title=_clean(item.get("title") or "(제목 없음)"),
        link=item["link"],
        published=_published(item),
        summary=_clean(summary)[:500],
        uid=item.get("id") or item.get("guid") or "",
    )


def _published(item) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        value = item.get(field)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _clean(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", _TAGS.sub(" ", text)).strip()
    for pattern in _BOILERPLATE:
        cleaned = pattern.sub("", cleaned).strip()
    return cleaned
