"""Safety guards: paper-only enforcement, HALT lock and run lock.

Every guard raises SafetyError; callers must let it propagate and stop
the program. Nothing here ever prints or logs secret values.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradingbot.config import PROJECT_ROOT

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
PAPER_ACCOUNT_PREFIX = "PA"

STATE_DIR = PROJECT_ROOT / "state"
HALT_LOCK_NAME = "HALT.lock"
RUN_LOCK_NAME = "run.lock"

# Env vars that, in the alpaca ecosystem, can redirect clients to another endpoint.
_ENDPOINT_ENV_VARS = ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL", "ALPACA_ENDPOINT")
_PAPER_FLAG_ENV_VARS = ("ALPACA_PAPER", "APCA_PAPER")


class SafetyError(RuntimeError):
    """Raised when continuing could touch a live account or violate a hard stop."""


def is_live_url(url: str) -> bool:
    """True if the URL points at Alpaca's live trading host (not the paper host)."""
    lowered = url.lower()
    return LIVE_HOST in lowered and PAPER_HOST not in lowered


def assert_paper_environment(env: Mapping[str, str] | None = None) -> None:
    """Abort if any environment variable configures a live endpoint or disables paper mode."""
    env = os.environ if env is None else env
    for name in _ENDPOINT_ENV_VARS:
        value = env.get(name, "")
        if value and is_live_url(value):
            raise SafetyError(f"{name} points to a LIVE Alpaca endpoint; only paper is allowed")
    for name in _PAPER_FLAG_ENV_VARS:
        value = env.get(name, "").strip().lower()
        if value in ("false", "0", "no", "off"):
            raise SafetyError(f"{name}={value} disables paper mode; only paper is allowed")


def assert_paper_base_url(base_url: object) -> None:
    """Abort unless the trading client actually targets the paper host.

    alpaca-py stores the URL as a `BaseURL` enum, whose str() is the member name,
    so the enum's `.value` is checked when present.
    """
    url = str(getattr(base_url, "value", base_url))
    if PAPER_HOST not in url.lower():
        raise SafetyError("Trading client is not using the paper endpoint")


def assert_paper_account(account_number: str) -> None:
    """Paper account numbers start with 'PA'; anything else is treated as live."""
    if not str(account_number).upper().startswith(PAPER_ACCOUNT_PREFIX):
        raise SafetyError("Account number does not look like a paper account; aborting")


def mask_account_number(account_number: str) -> str:
    """Show only the prefix and the last 4 characters (e.g. PA******1234)."""
    s = str(account_number)
    if len(s) <= 6:
        return s[:2] + "*" * max(len(s) - 2, 0)
    return s[:2] + "*" * (len(s) - 6) + s[-4:]


# --- HALT lock -----------------------------------------------------------------
# Written by the drawdown circuit breaker. Committed to git so it survives cloud runs.
# Only a human deletes it.


def halt_lock_path(state_dir: Path = STATE_DIR) -> Path:
    return state_dir / HALT_LOCK_NAME


def read_halt(state_dir: Path = STATE_DIR) -> dict[str, Any] | None:
    """Return the HALT lock contents, or None if the system is not halted."""
    path = halt_lock_path(state_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt lock still means "halted".
        return {"reason": "unreadable HALT.lock", "raw": True}


def write_halt(reason: str, details: Mapping[str, Any], state_dir: Path = STATE_DIR) -> Path:
    path = halt_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "details": dict(details),
        "how_to_resume": "Review the situation, then delete state/HALT.lock manually and commit.",
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def assert_not_halted(state_dir: Path = STATE_DIR) -> None:
    halt = read_halt(state_dir)
    if halt is not None:
        raise SafetyError(f"System is HALTED ({halt.get('reason', 'unknown')}); delete state/HALT.lock to resume")


# --- Run lock ------------------------------------------------------------------
# Prevents two runs at the same time. Local-only (git-ignored); stale after a TTL.


@dataclass
class RunLock:
    ttl: timedelta
    state_dir: Path = STATE_DIR

    @property
    def path(self) -> Path:
        return self.state_dir / RUN_LOCK_NAME

    def _is_stale(self, now: datetime) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(data["started_at"])
        except (OSError, ValueError, KeyError):
            return True
        return now - started > self.ttl

    def __enter__(self) -> RunLock:
        now = datetime.now(timezone.utc)
        if self.path.exists() and not self._is_stale(now):
            raise SafetyError("Another run is in progress (state/run.lock); refusing to start")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": now.isoformat(timespec="seconds")}) + "\n",
            encoding="utf-8",
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)
