"""구글 뉴스 섹션의 '이슈 전개' 추적.

구글 뉴스 섹션 RSS는 한 항목(<item>)이 기사 하나가 아니라 **같은 사건을 다룬
기사 묶음**이다. 설명(description)에 언론사별 기사가 <ol><li> 목록으로 들어온다.

이 묶음을 저장해 두고 다음 수집 때 비교한다.
  - 처음 보는 묶음        -> 새 이슈로 알림
  - 기존 묶음에 새 기사   -> 무엇이 추가됐는지만 알림
  - 달라진 게 없으면      -> 아무것도 보내지 않음

구글의 <guid> 는 이슈 번호가 아니라 **대표기사 ID** 다 (실측: 40개 항목 전부
guid == 대표기사 ID). 대표기사가 바뀌면 guid 도 바뀌므로 이슈 추적에 쓸 수 없다.
그래서 묶인 기사 링크가 겹치는지로 같은 이슈인지 판정한다.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
from bs4 import BeautifulSoup

from ..config import Source
from ..errors import SkipSource
from ..http import get
from ..models import Entry
from ..state import State

log = logging.getLogger(__name__)

FEED_URL = "https://news.google.com/rss/headlines/section/topic/{section}?hl={lang}&gl={country}&ceid={country}:{lang}"

# 한글 이름으로도 적을 수 있게 한다.
SECTIONS = {
    "세계": "WORLD", "국내": "NATION", "경제": "BUSINESS", "비즈니스": "BUSINESS",
    "기술": "TECHNOLOGY", "과학기술": "TECHNOLOGY", "IT": "TECHNOLOGY",
    "과학": "SCIENCE", "건강": "HEALTH", "스포츠": "SPORTS", "연예": "ENTERTAINMENT",
}
VALID_SECTIONS = {"WORLD", "NATION", "BUSINESS", "TECHNOLOGY", "SCIENCE",
                  "HEALTH", "SPORTS", "ENTERTAINMENT"}

MAX_LINKS_PER_ISSUE = 25   # 이슈당 기억할 기사 수. 상태 파일이 무한정 커지지 않게 한다.
DEFAULT_TTL_DAYS = 7       # 이 기간 동안 안 보인 이슈는 기록에서 지운다.
MAX_NEW_SHOWN = 4          # 알림 한 건에 표시할 새 기사 수


def fetch(source: Source, session, state: State) -> list[Entry]:
    section = _section(source)
    url = FEED_URL.format(
        section=section,
        lang=str(source.options.get("lang", "ko")),
        country=str(source.options.get("country", "KR")).upper(),
    )

    clusters = _parse(get(session, url).text)
    if not clusters:
        raise SkipSource(f"{section} 섹션에서 이슈를 읽지 못했습니다. 구글 뉴스 형식이 바뀌었을 수 있습니다.")

    stored: dict = state.get_meta(source.key, "issues") or {}
    now = datetime.now(timezone.utc)
    entries: list[Entry] = []

    for cluster in clusters:
        key = _match(cluster["fingerprints"], stored)

        if key is None:
            # 처음 보는 이슈
            key = "i:" + hashlib.sha1(cluster["lead_link"].encode("utf-8")).hexdigest()[:16]
            stored[key] = {
                "title": cluster["title"],
                "links": cluster["fingerprints"][:MAX_LINKS_PER_ISSUE],
                "last_seen": now.isoformat(timespec="seconds"),
                "updates": 0,
            }
            entries.append(_entry(cluster, badge="🆕 새 이슈", new_members=cluster["members"][1:],
                                  uid_seed=key, published=cluster["published"]))
            continue

        issue = stored[key]
        known = set(issue["links"])
        fresh = [m for m, fp in zip(cluster["members"], cluster["fingerprints"]) if fp not in known]

        issue["last_seen"] = now.isoformat(timespec="seconds")
        issue["title"] = cluster["title"]  # 대표 제목은 최신으로 갱신한다

        if not fresh:
            continue

        # 오래된 링크를 밀어내며 최근 것만 기억한다.
        issue["links"] = (issue["links"] + [fp for fp in cluster["fingerprints"] if fp not in known])[-MAX_LINKS_PER_ISSUE:]
        issue["updates"] = int(issue.get("updates", 0)) + 1

        entries.append(_entry(cluster, badge=f"🔄 전개 {issue['updates']}", new_members=fresh,
                              uid_seed=key + "|" + "|".join(sorted(m["fp"] for m in fresh)),
                              published=cluster["published"]))

    _prune(stored, now, int(source.options.get("issue_ttl_days", DEFAULT_TTL_DAYS)))
    state.set_meta(source.key, "issues", stored)
    log.info("[%s] 이슈 %d개 확인, 추적 중인 이슈 %d개", source.name, len(clusters), len(stored))
    return entries


def _section(source: Source) -> str:
    raw = str(source.options.get("section", "WORLD")).strip()
    section = SECTIONS.get(raw, raw.upper())
    if section not in VALID_SECTIONS:
        raise SkipSource(
            f"'{raw}' 는 모르는 섹션입니다. "
            f"가능한 값: {', '.join(sorted(SECTIONS))} (또는 {', '.join(sorted(VALID_SECTIONS))})"
        )
    return section


def _parse(raw: str) -> list[dict]:
    """섹션 피드를 이슈 묶음 목록으로 바꾼다."""
    clusters = []
    for item in feedparser.parse(raw).entries:
        if not item.get("link"):
            continue

        members = _members(item.get("summary", ""))
        if not members:
            # 묶음이 없는 단독 기사도 이슈 1건으로 취급한다.
            members = [{"title": _strip(item.get("title", "")), "link": item["link"], "publisher": ""}]
        for m in members:
            m["fp"] = hashlib.sha1(m["link"].encode("utf-8")).hexdigest()[:16]

        clusters.append({
            "title": _lead_title(item.get("title", "")),
            "lead_link": item["link"],
            "members": members,
            "fingerprints": [m["fp"] for m in members],
            "published": _published(item),
        })
    return clusters


def _members(summary_html: str) -> list[dict]:
    soup = BeautifulSoup(summary_html, "html.parser")
    members = []
    for li in soup.find_all("li"):
        anchor = li.find("a")
        if not anchor or not anchor.get("href"):
            continue
        font = li.find("font")
        members.append({
            "title": _lead_title(anchor.get_text(strip=True)),
            "link": anchor["href"],
            "publisher": font.get_text(strip=True) if font else "",
        })
    return members


def _entry(cluster: dict, badge: str, new_members: list[dict], uid_seed: str, published) -> Entry:
    shown = new_members[:MAX_NEW_SHOWN]
    lines = []
    if new_members:
        label = "새 기사" if "전개" in badge else "함께 보도된 기사"
        lines.append(f"{label} {len(new_members)}건:")
        lines.extend(f"· {m['title']}" + (f" ({m['publisher']})" if m["publisher"] else "") for m in shown)
        if len(new_members) > len(shown):
            lines.append(f"· … 외 {len(new_members) - len(shown)}건")

    return Entry(
        title=cluster["title"],
        link=cluster["lead_link"],
        published=published,
        uid=uid_seed,
        badge=badge,
        detail_lines=lines,
        dedup_by_title=False,  # 같은 이슈가 여러 번 전개될 수 있다
    )


def _match(fingerprints: list[str], stored: dict) -> str | None:
    """기사 링크가 가장 많이 겹치는 기존 이슈를 찾는다."""
    current = set(fingerprints)
    best_key, best_overlap = None, 0
    for key, issue in stored.items():
        overlap = len(current & set(issue.get("links", [])))
        if overlap > best_overlap:
            best_key, best_overlap = key, overlap
    return best_key


def _prune(stored: dict, now: datetime, ttl_days: int) -> None:
    cutoff = now - timedelta(days=ttl_days)
    for key, issue in list(stored.items()):
        try:
            last = datetime.fromisoformat(issue["last_seen"])
        except (KeyError, TypeError, ValueError):
            continue  # 날짜를 못 읽으면 보수적으로 남겨둔다
        if last < cutoff:
            del stored[key]


def _lead_title(text: str) -> str:
    """구글이 제목 끝에 붙이는 ' - 언론사' 를 떼어낸다."""
    title = _strip(text)
    head, sep, publisher = title.rpartition(" - ")
    return head if sep and len(publisher) <= 30 else title


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def _published(item):
    import calendar
    for field in ("published_parsed", "updated_parsed"):
        value = item.get(field)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None
