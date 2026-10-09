"""Read secrets from environment variables.

Locally, values come from `.env` (loaded only if the file exists and never
overriding variables already set). On the cloud they are plain env vars.
Secret values are never included in reprs or error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from tradingbot.config import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"


class MissingCredentialsError(RuntimeError):
    pass


def load_env_file() -> None:
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "AlpacaCredentials(<hidden>)"


def get_alpaca_credentials() -> AlpacaCredentials:
    load_env_file()
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    missing = [n for n, v in (("ALPACA_API_KEY", api_key), ("ALPACA_SECRET_KEY", secret_key)) if not v]
    if missing:
        raise MissingCredentialsError(f"Missing environment variables: {', '.join(missing)}")
    return AlpacaCredentials(api_key=api_key, secret_key=secret_key)


def get_discord_webhook_url() -> str | None:
    load_env_file()
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None
