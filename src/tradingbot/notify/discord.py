"""Discord webhook notifications. Sending can NEVER break a run.

- The webhook URL comes from DISCORD_WEBHOOK_URL (env / .env) and is never printed or
  logged: every logged text goes through `redact()` (the URL itself and any
  discord webhook pattern are replaced by <webhook>).
- Every failure (network error, timeout, HTTP error) is written to logs/notify.log and
  `send` returns False; nothing raises. HTTP 429 waits `retry_after` (capped), 5xx and
  network errors are retried up to `max_retries` times, other 4xx are not retried.
- Message content is cut to < 2000 characters (Discord's limit); mentions are disabled.
- `alert(level, message)` also prints to stderr, so an alert is visible even when
  Discord is disabled or down.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from tradingbot.config import PROJECT_ROOT, NotifyConfig

MAX_CONTENT = 1990            # Discord rejects content >= 2000 characters
MAX_RETRY_AFTER_SEC = 30.0
LOG_PATH = PROJECT_ROOT / "logs" / "notify.log"
_WEBHOOK_RE = re.compile(r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE)
LEVEL_PREFIX = {"warning": "⚠️ WARNING", "critical": "🚨 CRITICAL", "info": "ℹ️"}

Post = Callable[..., Any]


def truncate(text: str, limit: int = MAX_CONTENT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class DiscordNotifier:
    def __init__(self, url: str | None, cfg: NotifyConfig, log_path: Path = LOG_PATH,
                 post: Post = requests.post, sleep: Callable[[float], None] = time.sleep,
                 tag: str = "") -> None:
        self._url = url
        self.cfg = cfg
        self.log_path = log_path
        self._post = post
        self._sleep = sleep
        self.tag = tag              # e.g. "[DRY-RUN] " in front of alerts

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.discord_enabled and self._url)

    def redact(self, text: str) -> str:
        if self._url:
            text = text.replace(self._url, "<webhook>")
        return _WEBHOOK_RE.sub("<webhook>", text)

    def _log(self, message: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"{stamp} {self.redact(message)}\n")
        except Exception:  # noqa: BLE001 - logging must not raise either
            pass

    def send(self, content: str, attachment: Path | None = None) -> bool:
        """Post a message (and optionally one file). Returns True on success, never raises."""
        try:
            return self._send(content, attachment)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            self._log(f"send failed unexpectedly: {type(exc).__name__}: {exc}")
            return False

    def _send(self, content: str, attachment: Path | None) -> bool:
        if not self.enabled:
            self._log("skipped: discord disabled or DISCORD_WEBHOOK_URL not set")
            return False
        payload = {"content": truncate(content), "allowed_mentions": {"parse": []}}
        file_bytes = None
        if attachment is not None:
            try:
                file_bytes = attachment.read_bytes()
            except OSError as exc:
                self._log(f"attachment {attachment.name} unreadable ({exc}); sending text only")
        attempts = 1 + self.cfg.max_retries
        for attempt in range(1, attempts + 1):
            try:
                if file_bytes is not None:
                    response = self._post(self._url, data={"payload_json": json.dumps(payload)},
                                          files={"files[0]": (attachment.name, file_bytes, "text/markdown")},
                                          timeout=self.cfg.timeout_sec)
                else:
                    response = self._post(self._url, json=payload, timeout=self.cfg.timeout_sec)
            except Exception as exc:  # noqa: BLE001 - network errors, timeouts
                self._log(f"attempt {attempt}/{attempts}: {type(exc).__name__}: {exc}")
                if attempt < attempts:
                    self._sleep(float(attempt))
                continue
            status = int(getattr(response, "status_code", 0))
            if 200 <= status < 300:
                self._log(f"sent ({len(payload['content'])} chars"
                          + (f", attachment {attachment.name}" if file_bytes is not None else "") + ")")
                return True
            if status == 429 and attempt < attempts:
                wait = _retry_after(response)
                self._log(f"attempt {attempt}/{attempts}: HTTP 429, retry in {wait:.1f}s")
                self._sleep(wait)
                continue
            if status >= 500 and attempt < attempts:
                self._log(f"attempt {attempt}/{attempts}: HTTP {status}, retrying")
                self._sleep(float(attempt))
                continue
            self._log(f"attempt {attempt}/{attempts}: HTTP {status}: {str(getattr(response, 'text', ''))[:200]}")
            return False
        return False

    def alert(self, level: str, message: str) -> None:
        """Alert sink for the run (Executor / pipeline). stderr always, Discord when enabled."""
        try:
            print(f"ALERT[{level}] {message}", file=sys.stderr)
            logging.getLogger(__name__).warning("alert %s: %s", level, message)
        except Exception:  # noqa: BLE001
            pass
        self.send(f"{LEVEL_PREFIX.get(level, level)} {self.tag}{message}")


def _retry_after(response: Any) -> float:
    value: Any = None
    try:
        value = response.json().get("retry_after")
    except Exception:  # noqa: BLE001
        value = getattr(response, "headers", {}).get("Retry-After")
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SEC)
    except (TypeError, ValueError):
        return 1.0


def notifier_from_env(cfg: NotifyConfig, tag: str = "") -> DiscordNotifier:
    from tradingbot.credentials import get_discord_webhook_url

    return DiscordNotifier(get_discord_webhook_url(), cfg, tag=tag)
