"""네이버 뉴스 키워드 검색.

네이버 뉴스 RSS는 2022년 3월에 완전히 종료되어 더는 쓸 수 없다.
그래서 공식 검색 API를 쓴다 — 무료, 하루 25,000회, 1회당 최대 100건.

키 발급 (5분): https://developers.naver.com/apps/#/register
  → 애플리케이션 이름 아무거나, 사용 API 는 "검색" 선택
  → 발급된 Client ID / Client Secret 을 GitHub Secrets 에 등록
"""
from __future__ import annotations

import html
import logging
import os
import re
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from ..config import Source
from ..errors import SkipSource
from ..http import TIMEOUT
from ..models import Entry
from ..state import State

log = logging.getLogger(__name__)

SEARCH_URL = "https://openapi.naver.com/v1/search/news.json?query={query}&display={display}&sort=date"
MAX_DISPLAY = 100  # API 상한
_TAGS = re.compile(r"<[^>]+>")


def fetch(source: Source, session, state: State) -> list[Entry]:
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        raise SkipSource(
            "네이버 API 키가 없습니다. NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 을 "
            "GitHub Secrets 에 등록하거나, 이 소스를 enabled: false 로 바꿔 주세요."
        )

    display = min(int(source.options.get("display", 30)), MAX_DISPLAY)
    url = SEARCH_URL.format(query=quote(str(source.options["query"])), display=display)

    response = session.get(
        url,
        headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
        timeout=TIMEOUT,
    )
    if response.status_code == 401:
        raise SkipSource("네이버 API 키가 올바르지 않습니다. Secrets 값을 다시 확인해 주세요.")
    if response.status_code == 429:
        # 하루 25,000회를 넘긴 경우. 다음 실행에서 자연히 풀린다.
        raise SkipSource("네이버 API 하루 호출 한도를 넘었습니다.")
    response.raise_for_status()

    return [entry for item in response.json().get("items", []) if (entry := _to_entry(item))]


def _to_entry(item: dict) -> Entry | None:
    # 언론사 원문 주소가 있으면 그쪽이 같은 기사에 대해 더 안정적인 식별자다.
    link = item.get("originallink") or item.get("link")
    if not link:
        return None

    return Entry(
        title=_clean(item.get("title", "")) or "(제목 없음)",
        link=link,
        published=_published(item.get("pubDate")),
        summary=_clean(item.get("description", ""))[:500],
    )


def _published(raw: str | None):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _clean(text: str) -> str:
    """API 는 검색어를 <b> 로 감싸고 HTML 엔티티로 인코딩해서 돌려준다."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", text))).strip()
