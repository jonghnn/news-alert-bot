"""뉴스 키워드 감시. 구글 뉴스 RSS 검색을 쓴다 (무료, 키 불필요)."""
from __future__ import annotations

from urllib.parse import quote_plus

from ..config import Source
from ..models import Entry
from ..state import State
from . import feed

SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl={lang}&gl={country}&ceid={country}:{lang}"


def fetch(source: Source, session, state: State) -> list[Entry]:
    lang = str(source.options.get("lang", "ko"))
    country = str(source.options.get("country", "KR")).upper()
    url = SEARCH_URL.format(
        query=quote_plus(str(source.options["query"])),
        lang=lang,
        country=country,
    )

    entries = feed.parse(session, url)
    for entry in entries:
        # 구글 뉴스는 제목 끝에 " - 언론사" 를 붙인다. 언론사는 요약으로 옮긴다.
        title, separator, publisher = entry.title.rpartition(" - ")
        if separator and len(publisher) <= 30:
            entry.title = title
            entry.summary = publisher
    return entries
