"""텔레그램 전송."""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

from .models import Entry

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
SEND_INTERVAL = 1.2  # 텔레그램 권장 속도(초당 ~1건)를 넘지 않게 간격을 둔다.
MAX_LEN = 4000

ICONS = {"youtube": "🎬", "news": "📰", "naver": "🗞️", "site": "🌐", "issue": "🌏"}


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, session, token: str, chat_id: str, dry_run: bool = False):
        self.session = session
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self._last_sent = 0.0

    def _call(self, method: str, payload: dict) -> dict:
        response = self.session.post(API.format(token=self.token, method=method), json=payload, timeout=20)
        body = response.json() if response.content else {}
        if not body.get("ok"):
            raise TelegramError(f"{method} 실패: {body.get('description') or response.text[:200]}")
        return body["result"]

    def send(self, text: str, silent: bool = False, preview: bool = True) -> None:
        if self.dry_run:
            log.info("[전송 안 함] %s", text.replace("\n", " | ")[:200])
            return

        # 연속 전송 시 속도 제한에 걸리지 않도록 간격 유지
        wait = SEND_INTERVAL - (time.monotonic() - self._last_sent)
        if wait > 0:
            time.sleep(wait)

        self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text[:MAX_LEN],
                "parse_mode": "HTML",
                # 링크가 여러 개인 묶음 메시지는 미리보기를 끈다. 켜 두면 첫 기사
                # 썸네일 카드가 메시지 밑에 크게 붙어 목록을 가린다.
                "disable_web_page_preview": not preview,
                "disable_notification": silent,
            },
        )
        self._last_sent = time.monotonic()

    def send_entry(self, source_name: str, source_type: str, entry: Entry, tz, silent: bool) -> None:
        self.send(_format(source_name, source_type, entry, tz), silent=silent)

    def send_digest(self, pending, tz, silent: bool, by_topic: bool = False) -> int:
        """한 실행에서 모은 글을 묶어 한 메시지로 보낸다.

        30건이 30개의 알림으로 오는 걸 막는다. 길면 여러 메시지로 나눈다.

        by_topic 이면 소스 대신 주제(AI·반도체 / 주식·거시 / 코인)로 묶는다.
        소스 이름은 내가 설정한 수집 경로일 뿐이어서, 같은 반도체 기사가 'AI 뉴스'
        와 '기술 이슈' 로 흩어져 보였다. 읽는 쪽에서 의미가 있는 묶음은 주제다.
        """
        total = len(pending)

        if by_topic:
            rows = [
                {"topic": entry.topic or "기타", "title": entry.title, "link": entry.link, "badge": entry.badge}
                for _, entry in pending
            ]
            return self.send_rows(rows, silent)

        blocks = [[f"📬 <b>새 소식 {total}건</b>"]]
        grouped: dict[tuple[str, str], list] = {}
        for source, entry in pending:
            grouped.setdefault((source.name, source.type), []).append(entry)
        for (name, stype), entries in grouped.items():
            blocks.append(_digest_lines(name, stype, entries, tz))
        return self._send_blocks(blocks, silent)

    def send_rows(self, rows: list[dict], silent: bool) -> int:
        """주제별 제목 목록. Claude 브리핑이 안 왔을 때 대신 나가는 형식이다.

        rows: {"topic", "title", "link", "badge"} — picks.json 항목과 같은 모양.
        """
        blocks = [[f"📬 <b>새 소식 {len(rows)}건</b>"]]
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row.get("topic") or "기타", []).append(row)
        for name, items in grouped.items():  # 이미 주제 순서로 정렬돼 있다 (relevance.select)
            blocks.append(_topic_lines(name, items))
        return self._send_blocks(blocks, silent)

    def _send_blocks(self, blocks, silent: bool) -> int:

        # 텔레그램 상한에 맞춰 덩어리를 나눈다. 블록 중간은 자르지 않는다.
        messages: list[str] = []
        buf: list[str] = []
        for block in blocks:
            chunk = "\n".join(block)
            if buf and len("\n\n".join(buf)) + len(chunk) + 2 > MAX_LEN - 200:
                messages.append("\n\n".join(buf))
                buf = []
            buf.append(chunk)
        if buf:
            messages.append("\n\n".join(buf))

        for text in messages:
            self.send(text, silent=silent, preview=False)
        return len(messages)

    def get_updates(self) -> list[dict]:
        return self._call("getUpdates", {"timeout": 0})


def _format(source_name: str, source_type: str, entry: Entry, tz) -> str:
    icon = ICONS.get(source_type, "🔔")
    header = f"{icon} <b>{_text(source_name)}</b>"
    if entry.badge:
        header += f" · {_text(entry.badge)}"

    lines = [
        header,
        f'<a href="{html.escape(entry.link, quote=True)}">{_text(entry.title)}</a>',
    ]

    if entry.detail_lines:
        lines.extend(_text(line) for line in entry.detail_lines)
    elif entry.summary and entry.summary.lower() not in entry.title.lower():
        lines.append(f"<i>{_text(entry.summary[:200])}</i>")

    if entry.published:
        lines.append(_stamp(entry.published, tz))

    return "\n".join(lines)


TOPIC_ICONS = {
    "AI 활용·사례": "🛠️", "AI·반도체 산업": "🤖", "주식·거시": "📈", "코인": "🪙", "기타": "🔔",
}


TITLE_MAX = 45  # 한 줄에 들어가는 정도. 넘으면 자른다 (모바일에서 3줄씩 접히던 것)


def _topic_lines(topic_name: str, rows) -> list[str]:
    """주제 묶음 한 덩어리.

    가독성 (2026-11-16 "가독성이 너무 없음"): 예전에는 줄마다 제목 + 배지 +
    소스명(<i>AI 뉴스</i>)이 붙어 20줄이 한 덩어리로 보였다. 소스명은 읽는
    쪽에 쓸모가 없어 뺐고, 제목은 줄이고, 묶음 사이를 띄운다.
    """
    icon = TOPIC_ICONS.get(topic_name, "🔔")
    lines = [f"{icon} <b>{_text(topic_name)}</b>"]
    for row in rows:
        title = _short(_clean_title(row["title"]))
        lines.append(f'· <a href="{html.escape(row["link"], quote=True)}">{_text(title)}</a>')
    return lines


def _clean_title(title: str) -> str:
    """구글 뉴스 제목 끝의 ' - 언론사' 와 앞의 [말머리] 를 뗀다."""
    title = re.sub(r"\s+-\s+[^-]{1,25}$", "", title.strip())
    title = re.sub(r"^\s*\[[^\]]{1,20}\]\s*", "", title)
    return title


def _short(title: str) -> str:
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def _digest_lines(source_name: str, source_type: str, entries, tz) -> list[str]:
    icon = ICONS.get(source_type, "🔔")
    lines = [f"{icon} <b>{_text(source_name)}</b> ({len(entries)})"]
    for e in entries:
        badge = f" {_text(e.badge.split()[0])}" if e.badge else ""
        lines.append(f'· <a href="{html.escape(e.link, quote=True)}">{_text(e.title)}</a>{badge}')
    return lines


def _text(value: str) -> str:
    """텔레그램 HTML 본문용 이스케이프.

    quote=True(기본값)를 쓰면 따옴표까지 &#x27; 로 바뀌어 제목이 지저분해진다.
    본문에서는 < > & 만 처리하면 된다.
    """
    return html.escape(value, quote=False)


def _stamp(published: datetime, tz) -> str:
    local = published.astimezone(tz) if tz else published
    return f"<code>{local.strftime('%m/%d %H:%M')}</code>"
