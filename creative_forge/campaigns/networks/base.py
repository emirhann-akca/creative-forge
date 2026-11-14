"""What every network adapter does, and how an adapter is pointed at the mock, a sandbox or a live account.

Modes:

* ``mock``: the local mock network (``mock_server.py``). No credential is read; the token is the literal
  ``mock-token`` the mock expects.
* ``sandbox``: the network's own no-spend environment (Meta sandbox ad account, TikTok sandbox host, Google Ads test
  account). Credentials come from the environment. The adapter checks the account looks like a sandbox before any
  write and refuses otherwise.
* ``live``: a real ad account. Refused unless the caller passed ``allow_live`` (``--live`` on the command line).

Credentials are read from environment variables only, never from the YAML, a flag or a file, and the client's
redactor removes them from every log line and audit record.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from typing import Any, ClassVar

from creative_forge.campaigns.http import NetworkError, WriteClient, default_classify
from creative_forge.campaigns.plan import Op, PlanContext
from creative_forge.campaigns.state import CampaignRecord
from creative_forge.config import ConfigError
from creative_forge.perf.store import PerfRow

MOCK_TOKEN = "mock-token"


class SafetyError(RuntimeError):
    """A safety rule refused the action (wrong currency, not a sandbox, live without --live, a cap, ...)."""


def env_secret(name: str, what: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{what} needs {name} in the environment (never in the YAML, a flag or a file)")
    return value


class Adapter:
    network: ClassVar[str] = ""
    version: ClassVar[str] = ""
    live_base: ClassVar[str] = ""
    sandbox_base: ClassVar[str] = ""
    env: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        mode: str,
        account: str,
        settings: dict[str, Any],
        client: WriteClient,
        base_url: str,
        secrets: dict[str, str],
        sleep: Callable[[float], None],
    ) -> None:
        self.mode = mode
        self.account = account
        self.settings = settings
        self.client = client
        self.base = base_url.rstrip("/")
        self.secrets = secrets
        self.sleep = sleep

    # ---------------------------------------------------------------------------------------------- plan side
    @classmethod
    def account_of(cls, net: Any) -> str:
        raise NotImplementedError

    @classmethod
    def settings_of(cls, net: Any) -> dict[str, Any]:
        return {}

    @classmethod
    def build(cls, ctx: PlanContext, net: Any) -> tuple[list[Op], list[str]]:
        raise NotImplementedError

    @classmethod
    def load_secrets(cls, mode: str) -> dict[str, str]:
        raise NotImplementedError

    @staticmethod
    def classify(status: int, body: Any) -> tuple[str, str]:
        return default_classify(status, body)

    @classmethod
    def base_for(cls, mode: str, mock_url: str | None) -> str:
        if mode == "mock":
            if not mock_url:
                raise SafetyError("mock mode needs the local mock network")
            return f"{mock_url.rstrip('/')}/{cls.network}"
        return cls.sandbox_base if mode == "sandbox" else cls.live_base

    # ------------------------------------------------------------------------------------------- network side
    def preflight(self, currency: str) -> dict[str, Any]:
        """Read the account; refuse a currency mismatch, and in sandbox mode anything that is not a sandbox."""
        raise NotImplementedError

    def execute(self, op: Op, payload: dict[str, Any], ids: dict[str, str]) -> str:
        raise NotImplementedError

    def find(self, op: Op, ids: dict[str, str] | None = None) -> str | None:
        """The remote id of an object carrying ``op``'s key in its name, or None."""
        raise NotImplementedError

    def set_status(self, rec: CampaignRecord, active: bool) -> None:
        raise NotImplementedError

    def set_ad_status(self, rec: CampaignRecord, ad_id: str, active: bool) -> None:
        raise NotImplementedError

    def read(self, rec: CampaignRecord) -> dict[str, Any]:
        """``{"status": "ACTIVE" | "PAUSED", "daily": float, "spend": float, "currency": str, ...}``."""
        raise NotImplementedError

    def insights(self, rec: CampaignRecord, since: date, until: date) -> list[PerfRow]:
        raise NotImplementedError

    # -------------------------------------------------------------------------------------------------- helpers
    def _expect(self, cond: bool, message: str) -> None:
        if not cond:
            raise NetworkError(f"{self.network}: {message}")
