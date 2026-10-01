"""키워드 필터."""
from __future__ import annotations

import re
from functools import lru_cache

from .config import Keywords

_LATIN_ONLY = re.compile(r"[A-Za-z0-9]+")


@lru_cache(maxsize=512)
def _pattern(word: str) -> re.Pattern | None:
    """영문·숫자로만 된 키워드는 단어 경계를 요구한다.

    'AI' 를 부분 문자열로 찾으면 said / Thailand / email / main / detail / captain /
    Ukraine 에 전부 걸린다. 반면 한글은 조사가 붙어도 이어 쓰므로('반도체를', '주식은')
    부분 문자열이 맞다. 그래서 라틴 문자 키워드만 경계를 본다.

    경계를 라틴 문자·숫자에만 적용하므로 'AI반도체', '오픈AI' 는 정상적으로 걸린다.
    단 'OpenAI' 처럼 영문에 붙은 경우는 안 걸리니, 필요하면 키워드에 따로 추가한다.
    """
    if _LATIN_ONLY.fullmatch(word):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)
    return None


def _found(word: str, text: str, text_lower: str) -> bool:
    pattern = _pattern(word)
    if pattern is not None:
        return pattern.search(text) is not None
    return word.lower() in text_lower


def matches(text: str, keywords: Keywords) -> bool:
    """제외어가 하나라도 걸리면 탈락, 그다음 포함어 규칙을 본다."""
    lower = text.lower()

    for word in keywords.exclude:
        if _found(word, text, lower):
            return False

    if not keywords.include:
        return True

    hits = (_found(word, text, lower) for word in keywords.include)
    return all(hits) if keywords.mode == "all" else any(hits)
