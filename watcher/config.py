"""config.yml 읽기와 검증."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_TYPES = {"youtube", "news", "naver", "site", "issue"}

# 이 종류는 검색어(query) 자체가 필터다. 서버가 이미 걸러준 결과에
# 전역 include 를 또 씌우면 관련 기사가 대량으로 버려진다(실측: 100건 -> 22건).
#
# issue 는 여기 넣지 않는다. 섹션 하나에 이슈가 70개씩 들어와 '범위 지정'이라고
# 부르기 어렵고, include 를 안 걸면 관심 없는 이슈까지 전부 온다.
# 섹션 전체를 받고 싶으면 그 소스에 filter: false 를 적는다.
SERVER_FILTERED = {"news", "naver"}


class ConfigError(Exception):
    pass


@dataclass
class Keywords:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    mode: str = "any"

    @classmethod
    def parse(cls, raw: Any, where: str) -> "Keywords":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: keywords 는 include/exclude/mode 를 가진 항목이어야 합니다.")
        mode = str(raw.get("mode", "any")).lower()
        if mode not in {"any", "all"}:
            raise ConfigError(f"{where}: keywords.mode 는 any 또는 all 만 됩니다 (현재: {mode}).")
        return cls(
            include=[str(k) for k in raw.get("include") or []],
            exclude=[str(k) for k in raw.get("exclude") or []],
            mode=mode,
        )


@dataclass
class Source:
    name: str
    type: str
    key: str
    options: dict[str, Any]
    keywords: Keywords | None  # None 이면 전역 키워드 사용
    filter_mode: str  # full = include+exclude / exclude = 제외어만 / off = 필터 없음
    max_items: int
    max_age_days: int


@dataclass
class Config:
    keywords: Keywords
    sources: list[Source]
    timezone: str
    quiet_hours: tuple[int, int] | None


def _slug(name: str) -> str:
    """상태 파일의 키. 한글도 그대로 살려서 사람이 알아볼 수 있게 한다."""
    text = unicodedata.normalize("NFC", name).strip().lower()
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)) or "source"


def _parse_quiet_hours(raw: Any) -> tuple[int, int] | None:
    if not raw:
        return None
    match = re.fullmatch(r"\s*(\d{1,2}):\d{2}\s*-\s*(\d{1,2}):\d{2}\s*", str(raw))
    if not match:
        raise ConfigError('telegram.quiet_hours 형식은 "23:00-07:00" 이어야 합니다.')
    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ConfigError("telegram.quiet_hours 의 시(hour)는 0~23 이어야 합니다.")
    return start, end


def _filter_mode(item: dict, stype: str) -> str:
    """이 소스에 전역 키워드를 어디까지 적용할지 정한다."""
    raw = item.get("filter")
    if raw is False:
        return "off"
    if raw is True:
        return "full"
    if "keywords" in item:
        # 소스가 자기 키워드를 직접 적었다면 그건 쓰라는 뜻이다.
        return "full"
    # 검색형은 제외어만 적용한다. include 는 이미 query 가 한 일이다.
    return "exclude" if stype in SERVER_FILTERED else "full"


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yml 의 최상위는 항목 목록이어야 합니다.")

    defaults = raw.get("defaults") or {}
    default_max_items = int(defaults.get("max_items_per_source", 5))
    default_max_age = int(defaults.get("max_age_days", 7))

    telegram = raw.get("telegram") or {}

    raw_sources = raw.get("sources")
    if not raw_sources:
        raise ConfigError("config.yml 에 sources 가 하나도 없습니다.")

    sources: list[Source] = []
    used_keys: set[str] = set()
    for index, item in enumerate(raw_sources):
        where = f"sources[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: 각 소스는 name/type 을 가진 항목이어야 합니다.")
        if not item.get("enabled", True):
            continue

        name = str(item.get("name") or "").strip()
        stype = str(item.get("type") or "").strip().lower()
        if not name:
            raise ConfigError(f"{where}: name 이 필요합니다.")
        if stype not in VALID_TYPES:
            raise ConfigError(
                f"{where} ({name}): type 은 {', '.join(sorted(VALID_TYPES))} 중 하나여야 합니다 (현재: {stype or '없음'})."
            )

        if stype == "youtube" and not item.get("channel"):
            raise ConfigError(f"{where} ({name}): youtube 소스에는 channel 이 필요합니다.")
        if stype in {"news", "naver"} and not item.get("query"):
            raise ConfigError(f"{where} ({name}): {stype} 소스에는 query 가 필요합니다.")
        if stype == "site" and not item.get("url"):
            raise ConfigError(f"{where} ({name}): site 소스에는 url 이 필요합니다.")

        # 이름이 겹치면 상태가 섞이므로 키에 번호를 붙여 분리한다.
        key = f"{stype}:{_slug(name)}"
        if key in used_keys:
            key = f"{key}-{index}"
        used_keys.add(key)

        sources.append(
            Source(
                name=name,
                type=stype,
                key=key,
                options={k: v for k, v in item.items() if k not in {"name", "type", "enabled", "keywords", "filter"}},
                keywords=Keywords.parse(item.get("keywords"), f"{where} ({name})") if "keywords" in item else None,
                filter_mode=_filter_mode(item, stype),
                max_items=int(item.get("max_items", default_max_items)),
                max_age_days=int(item.get("max_age_days", default_max_age)),
            )
        )

    if not sources:
        raise ConfigError("활성화된(enabled: true) 소스가 없습니다.")

    return Config(
        keywords=Keywords.parse(raw.get("keywords"), "keywords"),
        sources=sources,
        timezone=str(telegram.get("timezone", "Asia/Seoul")),
        quiet_hours=_parse_quiet_hours(telegram.get("quiet_hours")),
    )
