"""일반 사이트 감시.

우선순위: 준 주소가 피드면 그대로 → 페이지에 걸린 RSS 자동 탐지 → selector 로 링크 긁기.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Source
from ..http import get
from ..models import Entry
from ..state import State
from . import feed

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "text/xml")


class ScrapeError(Exception):
    pass


def fetch(source: Source, session, state: State) -> list[Entry]:
    url = str(source.options["url"])
    selector = source.options.get("selector")

    response = get(session, url)
    content_type = response.headers.get("Content-Type", "").lower()

    # 1) 준 주소가 이미 피드인 경우
    if any(t in content_type for t in ("xml", "rss", "atom")) or response.content.lstrip()[:200].startswith(b"<?xml"):
        entries = feed.from_bytes(response.content)
        if entries:
            return entries

    soup = BeautifulSoup(response.text, "html.parser")

    # 2) selector 를 준 경우 크롤링을 우선한다 (사용자가 명시적으로 지정한 것이므로)
    if selector:
        return _scrape(soup, url, str(selector), source.name)

    # 3) <head> 에 걸린 RSS 자동 탐지
    feed_url = _discover_feed(soup, url, state, source)
    if feed_url:
        return feed.parse(session, feed_url)

    raise ScrapeError(
        f"{url} 에서 RSS를 찾지 못했습니다. "
        "config.yml 의 이 소스에 selector 를 추가하거나 RSS 주소를 직접 넣어 주세요."
    )


def _discover_feed(soup: BeautifulSoup, base_url: str, state: State, source: Source) -> str | None:
    cached = state.get_meta(source.key, "feed_url")
    if cached:
        return cached

    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        if link.get("type", "").lower() in FEED_TYPES and link.get("href"):
            feed_url = urljoin(base_url, link["href"])
            state.set_meta(source.key, "feed_url", feed_url)
            log.info("[%s] RSS 자동 탐지: %s", source.name, feed_url)
            return feed_url
    return None


def _scrape(soup: BeautifulSoup, base_url: str, selector: str, name: str) -> list[Entry]:
    nodes = soup.select(selector)
    if not nodes:
        raise ScrapeError(f"selector '{selector}' 에 걸리는 요소가 없습니다. 사이트 구조가 바뀌었을 수 있습니다.")

    entries: list[Entry] = []
    for node in nodes:
        anchor = node if node.name == "a" else node.find("a")
        if not anchor or not anchor.get("href"):
            continue
        title = re.sub(r"\s+", " ", anchor.get_text(strip=True))
        if not title:
            continue
        entries.append(Entry(title=title, link=urljoin(base_url, anchor["href"])))

    if not entries:
        raise ScrapeError(f"selector '{selector}' 로 링크를 못 뽑았습니다. 링크(<a>)를 포함한 선택자인지 확인해 주세요.")

    log.info("[%s] 크롤링으로 %d건 수집", name, len(entries))
    return entries
