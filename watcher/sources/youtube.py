"""유튜브 채널 감시. API 키 없이 공개 RSS 피드만 쓴다."""
from __future__ import annotations

import logging
import re

from ..config import Source
from ..errors import SkipSource
from ..http import TIMEOUT, get
from ..models import Entry
from ..state import State
from . import feed

log = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")

# ⚠️ 순서가 중요하다. 채널 페이지에는 '추천 채널' 들의 ID 도 잔뜩 들어있어서,
#    느슨한 패턴을 먼저 쓰면 엉뚱한 채널을 조용히 가져온다.
#    실제로 "channelId" 패턴은 한 페이지에서 서로 다른 ID 12개를 잡았고,
#    그 결과 '조코딩' 대신 '멋쟁이사자처럼' 을 수집한 적이 있다.
#    canonical / og:url / externalId / browseId 는 그 채널 자신만 가리킨다.
IN_PAGE = (
    re.compile(r'<link rel="canonical" href="[^"]*?/channel/(UC[\w-]{22})"'),
    re.compile(r'<meta property="og:url" content="[^"]*?/channel/(UC[\w-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[\w-]{22})"'),
    re.compile(r'"browseId"\s*:\s*"(UC[\w-]{22})"'),
)


def fetch(source: Source, session, state: State) -> list[Entry]:
    channel_id = _resolve(source, session, state)
    response = get(session, FEED_URL.format(channel_id=channel_id))

    # 어느 채널을 실제로 가져왔는지 남긴다. 설정한 이름과 다르면 여기서 보인다.
    actual = re.search(r"<title>(.*?)</title>", response.text)
    if actual:
        log.info("[%s] 피드 채널명: %s", source.name, actual.group(1))

    return feed.from_bytes(response.content)


def _resolve(source: Source, session, state: State) -> str:
    """@핸들이나 채널 주소를 채널 ID로 바꾼다. 한 번 찾으면 상태에 캐시한다."""
    raw = str(source.options["channel"]).strip()

    if CHANNEL_ID.match(raw):
        return raw

    match = re.search(r"channel/(UC[\w-]{22})", raw)
    if match:
        return match.group(1)

    cached = state.get_meta(source.key, "channel_id")
    if cached:
        return cached

    url = raw if raw.startswith("http") else f"https://www.youtube.com/{raw.lstrip('/')}"
    log.info("[%s] 채널 ID를 찾는 중: %s", source.name, url)
    html = get(session, url).text

    for pattern in IN_PAGE:
        found = pattern.search(html)
        if not found:
            continue
        channel_id = found.group(1)
        # 열리는 것만 캐시한다. 잘못된 ID 를 캐시하면 계속 그걸 쓴다.
        probe = session.get(FEED_URL.format(channel_id=channel_id), timeout=TIMEOUT)
        if probe.status_code != 200:
            log.warning("[%s] %s 는 피드가 열리지 않습니다 (%s). 다음 패턴을 시도합니다.",
                        source.name, channel_id, probe.status_code)
            continue
        state.set_meta(source.key, "channel_id", channel_id)
        log.info("[%s] 채널 ID 확인: %s", source.name, channel_id)
        return channel_id

    raise SkipSource(
        f"채널 ID를 찾지 못했습니다: {raw}. "
        "유튜브 RSS 는 요청이 몰리면 404 를 돌려줍니다(일시적). 계속 실패하면 "
        "채널 페이지 → 정보 → 공유 에서 얻은 UC로 시작하는 ID를 config.yml 에 직접 넣어 보세요."
    )
