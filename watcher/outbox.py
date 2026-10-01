"""요약 브리핑을 주고받는 통로.

Claude 루틴은 텔레그램 토큰을 갖고 있지 않다(일부러 주지 않는다). 대신
`state/outbox/` 에 메시지 파일을 써서 커밋하면, 다음 Actions 실행이 그걸
집어 보내고 지운다. 토큰은 GitHub Secrets 한 곳에만 남는다.

그리고 루틴이 "무엇이 바뀌었는지" 를 쓰려면 봇이 뭘 보냈는지 알아야 한다.
매 전송을 `state/sent-log.jsonl` 에 한 줄씩 남겨 두면, 루틴이 뉴스를
처음부터 다시 받지 않아도 된다 (입력 토큰 27K → 2~5K).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

LOG_RETENTION_DAYS = 7
MAX_MESSAGE_CHARS = 3500   # 텔레그램 상한(4096)보다 여유 있게


def log_sent(state_path: str | Path, source_name: str, source_type: str, entry) -> None:
    """보낸 글 한 건을 기록한다. 루틴이 이걸 읽고 요약한다."""
    path = Path(state_path).parent / "sent-log.jsonl"
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_name,
        "type": source_type,
        "badge": entry.badge,
        "title": entry.title,
        "link": entry.link,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as error:
        # 기록 실패가 알림을 막아서는 안 된다.
        log.warning("전송 로그를 남기지 못했습니다: %s", error)


def prune_log(state_path: str | Path) -> None:
    """오래된 기록을 지운다. 커밋되는 파일이라 무한정 커지면 안 된다."""
    path = Path(state_path).parent / "sent-log.jsonl"
    if not path.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            if datetime.fromisoformat(json.loads(line)["at"]) > cutoff:
                kept.append(line)
        except (ValueError, KeyError, json.JSONDecodeError):
            kept.append(line)  # 못 읽는 줄은 보수적으로 남긴다
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def flush(telegram, state_path: str | Path, silent: bool) -> int:
    """outbox 에 쌓인 메시지를 보내고 지운다.

    보낸 뒤에 지우므로, 전송이 실패하면 파일이 남아 다음 실행에서 재시도된다.
    """
    box = Path(state_path).parent / "outbox"
    if not box.is_dir():
        return 0

    sent = 0
    for item in sorted(box.glob("*.md")):
        try:
            text = item.read_text(encoding="utf-8").strip()
        except OSError as error:
            log.warning("outbox 파일을 읽지 못했습니다 %s: %s", item.name, error)
            continue
        if not text:
            _discard(item, telegram)
            continue

        telegram.send(text[:MAX_MESSAGE_CHARS], silent=silent)
        # 보낸 뒤에만 지운다. 전송이 실패하면 파일이 남아 다음 실행에서 재시도된다.
        _discard(item, telegram)
        sent += 1
        log.info("브리핑 전송: %s", item.name)

    return sent


def _discard(item: Path, telegram) -> None:
    """dry-run 에서는 지우지 않는다. 안 보냈는데 지우면 브리핑이 증발한다."""
    if getattr(telegram, "dry_run", False):
        log.info("[지우지 않음] %s (dry-run)", item.name)
        return
    item.unlink(missing_ok=True)
