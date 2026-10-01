"""소스 종류별 수집기."""
from __future__ import annotations

from typing import Callable

from ..config import Source
from ..models import Entry
from ..state import State
from . import issue, naver, news, site, youtube

Fetcher = Callable[..., list[Entry]]

FETCHERS: dict[str, Fetcher] = {
    "youtube": youtube.fetch,
    "news": news.fetch,
    "naver": naver.fetch,
    "issue": issue.fetch,
    "site": site.fetch,
}


def fetch(source: Source, session, state: State) -> list[Entry]:
    return FETCHERS[source.type](source, session, state)
