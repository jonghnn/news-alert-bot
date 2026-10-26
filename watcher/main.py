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
from datetime import datetime, time as clock, timedelta, timezone

from . import config as config_module
from . import outbox
from . import relevance
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

    # 전송 시각이 아니면 수집조차 하지 않는다. 수집만 하고 안 보내면 이슈 전개
    # 기록이 갱신되면서 '무엇이 새로 붙었는지' 가 영구히 사라진다.
    if not args.now and _delivery_slot(cfg.delivery_times, tz, state) is None:
        log.info(
            "지금은 전송 시각이 아닙니다. 다음 전송 예정: %s (설정: %s)",
            _next_slot_text(cfg.delivery_times, tz),
            ", ".join(f"{h:02d}:{m:02d}" for h, m in cfg.delivery_times),
        )
        return 0

    state.prune({source.key for source in cfg.sources})
    silent = _in_quiet_hours(cfg.quiet_hours, tz)

    # 루틴이 써 둔 요약 브리핑을 먼저 내보낸다.
    sent_total = 0
    failed: list[str] = []
    try:
        sent_total += outbox.flush(telegram, args.state, silent)
    except Exception as error:
        log.error("브리핑 전송 실패: %s", error)

    # 소스별로 바로 보내지 않고 모았다가 한 번에 보낸다 (묶음 전송).
    pending: list[tuple] = []
    try:
        for source in cfg.sources:
            try:
                pending += [(source, entry) for entry in _collect(source, cfg, session, state)]
                state.set_meta(source.key, "failures", 0)
            except SkipSource as reason:
                # 설정이 덜 된 것은 고장이 아니므로 실패로 세지 않는다.
                log.warning("[%s] 건너뜀 — %s", source.name, reason)
            except Exception as error:  # 한 소스가 죽어도 나머지는 계속 돌아야 한다.
                failed.append(source.name)
                log.error("[%s] 실패: %s", source.name, error)
                _maybe_alert(source, telegram, state, error)

        if pending and cfg.relevance:
            before = len(pending)
            pending, dropped, by_topic = relevance.select(pending, cfg)
            for source, entry in dropped:
                _remember(state, source.key, entry)
            log.info(
                "관련성 점수로 %d건 중 %d건을 골랐습니다 (%d점 미만·중복·할당 초과 %d건 버림)",
                before, len(pending), cfg.relevance_min_score, len(dropped),
            )
            if by_topic:
                log.info("주제별: %s", ", ".join(f"{k} {v}건" for k, v in by_topic.items()))
        elif pending and cfg.max_per_delivery:
            pending, dropped = _trim(pending, cfg.max_per_delivery, state)
            if dropped:
                log.info("전체 상한을 넘겨 %d건을 버립니다 (한 번에 최대 %d건)", dropped, cfg.max_per_delivery)

        if pending:
            sent_total += _deliver(pending, cfg, telegram, state, tz, silent, args.state)
    finally:
        # 이번 차례를 썼다고 기록한다. 안 그러면 다음 예약 시각까지 매 실행이 또 보낸다.
        state.set_global("last_delivery", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # 전송 도중 중단되더라도 이미 보낸 건 다시 안 가도록 반드시 저장한다.
        state.save()
        outbox.prune_log(args.state)

    log.info("완료: %d건 전송, 소스 %d개 중 %d개 실패", sent_total, len(cfg.sources), len(failed))
    if failed:
        log.warning("실패한 소스: %s", ", ".join(failed))

    return 1 if len(failed) == len(cfg.sources) else 0


def _collect(source, cfg, session, state) -> list:
    """이 소스에서 보낼 글을 고른다. 전송은 하지 않는다 (_deliver 가 한다)."""
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
        return []

    # 관련성 점수를 쓸 때는 여기서 자르지 않는다.
    #
    # ⚠️ 여기서 '최신순 N건' 으로 먼저 자르면 점수 심사가 무의미해진다. 실측:
    #    386건을 모았는데 이 단계에서 33건만 남아 그 33건 중 17건을 골랐다.
    #    없애려던 '최신순 선택' 이 앞단에 그대로 남아 있었던 셈이고, 점수가 높은
    #    기사가 _remember() 로 영구히 버려졌다.
    #    소스당 상한은 점수를 본 뒤에 relevance.max_per_source 가 적용한다.
    if cfg.relevance:
        fresh.sort(key=_when)
        return fresh

    # 넘치는 건 '미루지' 않고 버린다.
    #
    # 예전에는 상한을 넘으면 다음 실행으로 미뤘다. 그런데 공급이 상한보다 10배
    # 많아서(실측: 한 실행에 통과 260건 / 전송 28건 / 적체 208건) 큐가 영구히
    # 가득 찬 상태가 됐다. 그래서 간격이 2시간이든 7시간이든 매번 28건이 나갔고,
    # 새벽 3시에 오는 것도 '새벽 뉴스' 가 아니라 몇 시간 전 기사였다.
    # 최신을 남기고 나머지는 본 것으로 기록해 다시 오지 않게 한다.
    fresh.sort(key=_when, reverse=True)
    if len(fresh) > source.max_items:
        for entry in fresh[source.max_items:]:
            _remember(state, source.key, entry)
        log.info(
            "[%s] %d건은 버립니다 (소스당 최대 %d건, 최신 우선)",
            source.name, len(fresh) - source.max_items, source.max_items,
        )
        fresh = fresh[: source.max_items]

    # 보낼 때는 오래된 순이어야 알림이 시간 순서대로 도착한다.
    fresh.sort(key=_when)
    return fresh


def _when(entry) -> datetime:
    """정렬용 시각. 날짜가 없는 글은 가장 오래된 것으로 취급한다."""
    return entry.published or datetime.min.replace(tzinfo=timezone.utc)


def _trim(pending, cap: int, state) -> tuple[list, int]:
    """전체 상한에 맞춘다.

    소스를 번갈아 돌며 최신부터 한 건씩 뽑는다. 그냥 전체를 최신순으로 자르면
    글이 많은 소스(코인 뉴스는 한 번에 100건)가 상한을 전부 차지해서 경제 이슈가
    한 건도 안 올 수 있다.
    """
    by_source: dict[str, list] = {}
    for source, entry in pending:
        by_source.setdefault(source.key, []).append((source, entry))
    for rows in by_source.values():
        rows.sort(key=lambda pair: _when(pair[1]), reverse=True)

    keep: list = []
    while len(keep) < cap:
        took = False
        for rows in by_source.values():
            if not rows:
                continue
            keep.append(rows.pop(0))
            took = True
            if len(keep) >= cap:
                break
        if not took:
            break

    dropped = [pair for rows in by_source.values() for pair in rows]
    for source, entry in dropped:
        _remember(state, source.key, entry)

    keep.sort(key=lambda pair: _when(pair[1]))
    return keep, len(dropped)


def _delivery_slot(times, tz, state) -> datetime | None:
    """지금이 보낼 차례인지 본다. 차례면 그 예약 시각을 돌려준다.

    '08:30 에 보낸다' 가 아니라 '08:30 이 지난 뒤 첫 실행에서 보낸다' 다.
    GitHub 이 08:30 실행을 버려도(실측 96회/일 중 6.5회만 실행) 다음 실행이
    대신 보내므로 하루 전송 횟수가 보장된다.
    """
    if not times:
        return datetime.now(tz)  # 설정이 없으면 예전처럼 매 실행 전송

    now = datetime.now(tz)
    passed = [
        slot
        for day in (0, 1)
        for h, m in times
        for slot in [datetime.combine(now.date() - timedelta(days=day), clock(h, m), tzinfo=tz)]
        if slot <= now
    ]
    if not passed:
        return None
    latest = max(passed)

    last = state.get_global("last_delivery")
    if last:
        try:
            if datetime.fromisoformat(last).astimezone(tz) >= latest:
                return None
        except ValueError:
            pass  # 기록이 깨졌으면 보낸다. 빠뜨리는 쪽이 더 나쁘다.
    return latest


def _next_slot_text(times, tz) -> str:
    if not times:
        return "매 실행"
    now = datetime.now(tz)
    upcoming = [
        slot
        for day in (0, 1)
        for h, m in times
        for slot in [datetime.combine(now.date() + timedelta(days=day), clock(h, m), tzinfo=tz)]
        if slot > now
    ]
    return min(upcoming).strftime("%m/%d %H:%M")


def _deliver(pending, cfg, telegram, state, tz, silent, state_path) -> int:
    """모아둔 글을 보낸다.

    digest 가 켜져 있으면 소스별로 묶어 한 메시지로 보낸다. 30건이 30개의
    알림으로 쏟아지는 걸 막는다. 꺼져 있으면 예전처럼 한 건씩 보낸다.
    """
    if cfg.digest:
        # 묶음은 통째로 성공해야 기록한다. 실패하면 다음 실행에서 다시 보낸다.
        count = telegram.send_digest(pending, tz, silent, by_topic=cfg.group_by_topic and cfg.relevance)
        for source, entry in pending:
            _remember(state, source.key, entry)
            outbox.log_sent(state_path, source.name, source.type, entry)
        log.info("묶음 %d개 메시지로 %d건 전송", count, len(pending))
        return len(pending)

    for source, entry in pending:
        telegram.send_entry(source.name, source.type, entry, tz, silent)
        _remember(state, source.key, entry)
        outbox.log_sent(state_path, source.name, source.type, entry)
        log.info("[%s] 전송: %s", source.name, entry.title[:60])
    return len(pending)


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
    parser.add_argument("--now", action="store_true", help="전송 시각을 무시하고 지금 보낸다 (수동 실행용)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
