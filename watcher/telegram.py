"""텔레그램 전송."""
from __future__ import annotations

import html
import logging
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

    def send(self, text: str, silent: bool = False) -> None:
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
                "disable_web_page_preview": False,
                "disable_notification": silent,
            },
        )
        self._last_sent = time.monotonic()

    def send_entry(self, source_name: str, source_type: str, entry: Entry, tz, silent: bool) -> None:
        self.send(_format(source_name, source_type, entry, tz), silent=silent)

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


def _text(value: str) -> str:
    """텔레그램 HTML 본문용 이스케이프.

    quote=True(기본값)를 쓰면 따옴표까지 &#x27; 로 바뀌어 제목이 지저분해진다.
    본문에서는 < > & 만 처리하면 된다.
    """
    return html.escape(value, quote=False)


def _stamp(published: datetime, tz) -> str:
    local = published.astimezone(tz) if tz else published
    return f"<code>{local.strftime('%m/%d %H:%M')}</code>"
