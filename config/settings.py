from __future__ import annotations

import yaml
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8"}

    # Hyperliquid
    hyperliquid_wallet_address: str = ""
    hyperliquid_api_private_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Network
    network: str = Field(default="testnet")

    # Mainnet safety: must be "true" to place orders on mainnet
    confirm_mainnet: str = Field(default="")

    # Bankroll
    initial_bankroll: float = Field(default=1000.0)

    @property
    def is_mainnet_confirmed(self) -> bool:
        return self.network != "mainnet" or self.confirm_mainnet.lower() == "true"


def load_strategy() -> dict[str, Any]:
    path = CONFIG_DIR / "strategy.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def save_strategy(data: dict[str, Any]) -> None:
    path = CONFIG_DIR / "strategy.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def load_blacklist() -> list[str]:
    path = CONFIG_DIR / "blacklist.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("blacklist", [])


settings = Settings()
