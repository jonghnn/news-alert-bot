"""알림 봇 진입점.

  python -m watcher.main              # 평소 실행
  python -m watcher.main --dry-run    # 보낼 내용만 로그로 확인, 실제 전송 안 함
  python -m watcher.main --mode test  # 연결 확인용 테스트 메시지 1건
  python -m watcher.main --mode chatid  # 내 chat_id 확인
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import config as config_module
from .config import Keywords
from . import http, sources
from .errors import SkipSource
from .filters import matches
from .state import State
from .telegram import Telegram, TelegramError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 파이썬 3.8 이하
    ZoneInfo = None

log = logging.getLogger("watcher")

# 같은 소스가 연속으로 이만큼 실패하면 텔레그램으로 한 번 알린다 (조용히 죽는 것 방지).
FAILURE_ALERT_THRESHOLD = 3


def _force_utf8() -> None:
    """윈도우 콘솔 기본 인코딩(cp949)에서는 한글 로그가 깨진다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN 이 없습니다. GitHub 저장소 Settings → Secrets 에 등록해 주세요.")
        return 1

    session = http.make_session()

    if args.mode == "chatid":
        return _print_chat_ids(session, token)

    if not chat_id:
        log.error("TELEGRAM_CHAT_ID 가 없습니다. --mode chatid 로 확인한 값을 Secrets 에 등록해 주세요.")
        return 1

    telegram = Telegram(session, token, chat_id, dry_run=args.dry_run)

    if args.mode == "test":
        telegram.send("✅ 알림 봇 연결 성공. 이제 새 글이 올라오면 여기로 보내드립니다.")
        log.info("테스트 메시지를 보냈습니다.")
        return 0

    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as error:
        log.error("설정 오류: %s", error)
        return 1

    tz = _timezone(cfg.timezone)
    state = State(args.state)
    state.prune({source.key for source in cfg.sources})
    silent = _in_quiet_hours(cfg.quiet_hours, tz)

    sent_total = 0
    failed: list[str] = []

    try:
        for source in cfg.sources:
            try:
                sent_total += _process(source, cfg, telegram, session, state, tz, silent)
                state.set_meta(source.key, "failures", 0)
            except SkipSource as reason:
                # 설정이 덜 된 것은 고장이 아니므로 실패로 세지 않는다.
                log.warning("[%s] 건너뜀 — %s", source.name, reason)
            except Exception as error:  # 한 소스가 죽어도 나머지는 계속 돌아야 한다.
                failed.append(source.name)
                log.error("[%s] 실패: %s", source.name, error)
                _maybe_alert(source, telegram, state, error)
    finally:
        # 전송 도중 중단되더라도 이미 보낸 건 다시 안 가도록 반드시 저장한다.
        state.save()

    log.info("완료: %d건 전송, 소스 %d개 중 %d개 실패", sent_total, len(cfg.sources), len(failed))
    if failed:
        log.warning("실패한 소스: %s", ", ".join(failed))

    return 1 if len(failed) == len(cfg.sources) else 0


def _process(source, cfg, telegram, session, state, tz, silent) -> int:
    entries = sources.fetch(source, session, state)
    log.info("[%s] %d건 수집", source.name, len(entries))

    keywords = source.keywords or cfg.keywords
    if source.filter_mode == "exclude":
        # 검색어가 이미 1차로 걸렀으니 제외어만 본다.
        keywords = Keywords(include=[], exclude=keywords.exclude, mode=keywords.mode)
    cutoff = datetime.now(timezone.utc) - timedelta(days=source.max_age_days)

    fresh = []
    batch_keys: set[str] = set()  # 같은 실행 안에서 중복된 기사도 걸러낸다
    for entry in entries:
        keys = entry.dedup_keys
        if any(state.has_seen(source.key, key) or key in batch_keys for key in keys):
            continue
        if entry.published and entry.published < cutoff:
            continue
        if source.filter_mode != "off" and not matches(entry.haystack, keywords):
            continue
        fresh.append(entry)
        batch_keys.update(keys)

    # 첫 실행에서는 과거 글이 한꺼번에 쏟아지므로 전송 없이 기록만 남긴다.
    if not state.is_initialized(source.key):
        for entry in entries:
            _remember(state, source.key, entry)
        state.mark_initialized(source.key)
        log.info("[%s] 첫 등록 — 기존 글 %d건은 건너뜁니다. 다음 글부터 알림이 갑니다.", source.name, len(entries))
        return 0

    # 오래된 글부터 보내야 알림이 시간 순서대로 도착한다.
    fresh.sort(key=lambda e: e.published or datetime.min.replace(tzinfo=timezone.utc))

    skipped = len(fresh) - source.max_items
    if skipped > 0:
        log.info("[%s] %d건은 다음 실행으로 미룹니다 (소스당 최대 %d건)", source.name, skipped, source.max_items)
        fresh = fresh[: source.max_items]

    for entry in fresh:
        telegram.send_entry(source.name, source.type, entry, tz, silent)
        _remember(state, source.key, entry)
        log.info("[%s] 전송: %s", source.name, entry.title[:60])

    return len(fresh)


def _remember(state, source_key: str, entry) -> None:
    for key in entry.dedup_keys:
        state.mark_seen(source_key, key)


def _maybe_alert(source, telegram, state, error) -> None:
    """반복 실패는 사용자가 알아야 한다. 매번 보내면 시끄러우니 임계치에서 한 번만."""
    failures = int(state.get_meta(source.key, "failures") or 0) + 1
    state.set_meta(source.key, "failures", failures)

    if failures != FAILURE_ALERT_THRESHOLD:
        return

    try:
        telegram.send(
            f"⚠️ <b>{source.name}</b> 소스를 {failures}회 연속 가져오지 못했습니다.\n"
            f"<code>{str(error)[:300]}</code>",
            silent=True,
        )
    except TelegramError as send_error:
        log.error("실패 알림 전송 실패: %s", send_error)


def _print_chat_ids(session, token: str) -> int:
    """봇에게 아무 메시지나 보낸 뒤 실행하면 chat_id 가 로그에 찍힌다."""
    telegram = Telegram(session, token, chat_id="", dry_run=False)
    try:
        updates = telegram.get_updates()
    except TelegramError as error:
        log.error("%s", error)
        return 1

    found = {
        str(chat["id"]): chat.get("title") or chat.get("username") or chat.get("first_name", "")
        for update in updates
        for chat in [(update.get("message") or update.get("channel_post") or {}).get("chat")]
        if chat
    }

    if not found:
        log.error("대화 기록이 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행해 주세요.")
        return 1

    log.info("찾은 chat_id 목록:")
    for cid, name in found.items():
        log.info("  chat_id = %s   (%s)", cid, name)
    return 0


def _timezone(name: str):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("시간대 '%s' 를 알 수 없어 UTC 를 씁니다.", name)
        return timezone.utc


def _in_quiet_hours(quiet_hours, tz) -> bool:
    if not quiet_hours:
        return False
    start, end = quiet_hours
    hour = datetime.now(tz).hour
    # 23시~7시처럼 자정을 넘는 구간도 처리한다.
    return start <= hour < end if start < end else (hour >= start or hour < end)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="관심사 알림 봇")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--state", default="state/seen.json")
    parser.add_argument("--mode", choices=["run", "test", "chatid"], default="run")
    parser.add_argument("--dry-run", action="store_true", help="실제 전송 없이 결과만 로그로 출력")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
