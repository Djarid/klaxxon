"""Configuration loader.

Loads config.yaml for escalation patterns and .env for secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .services.reminder_engine import EscalationConfig, EscalationStage


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env loader. No dependencies."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                env[key] = value
    return env


@dataclass
class AppConfig:
    """Application configuration."""

    # Signal
    signal_api_url: str = "http://localhost:8082"
    signal_account: str = ""
    signal_recipient: str = ""

    # API
    bearer_token: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8443

    # Database
    db_path: str = ":memory:"

    # TLS
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None

    # Timezone
    timezone: str = "Europe/London"

    # Escalation
    escalation: EscalationConfig = field(
        default_factory=lambda: EscalationConfig(stages=[])
    )

    # Scheduler
    check_interval_sec: int = 60

    # Signal commands
    ack_keywords: list[str] = field(default_factory=lambda: ["ack", "joining"])
    skip_keywords: list[str] = field(default_factory=lambda: ["skip"])
    list_keywords: list[str] = field(default_factory=lambda: ["list", "meetings"])
    help_keywords: list[str] = field(default_factory=lambda: ["help"])


def load_config(
    config_path: Path = Path("config.yaml"),
    env_path: Optional[Path] = None,
) -> AppConfig:
    """Load configuration from config.yaml and .env."""
    # Load .env
    if env_path is None:
        env_path = config_path.parent / ".env"
    dotenv = _load_dotenv(env_path)
    for k, v in dotenv.items():
        os.environ.setdefault(k, v)

    cfg = AppConfig()

    # Load config.yaml
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        cfg.timezone = data.get("timezone", cfg.timezone)

        # Parse escalation
        esc_data = data.get("escalation", {})
        stages = []
        for s in esc_data.get("stages", []):
            stages.append(
                EscalationStage(
                    offset_hours=s["offset_hours"],
                    interval_min=s.get("interval_min"),
                    message=s["message"],
                )
            )
        cfg.escalation = EscalationConfig(
            stages=stages,
            post_start_interval_min=esc_data.get("post_start_interval_min", 2),
            post_start_message=esc_data.get(
                "post_start_message", cfg.escalation.post_start_message
            ),
            timeout_after_min=esc_data.get("timeout_after_min", 90),
        )

        # Scheduler
        sched = data.get("scheduler", {})
        cfg.check_interval_sec = sched.get("check_interval_sec", cfg.check_interval_sec)

        # Signal commands
        cmds = data.get("commands", {})
        if "acknowledge" in cmds:
            cfg.ack_keywords = cmds["acknowledge"]
        if "skip" in cmds:
            cfg.skip_keywords = cmds["skip"]
        if "list" in cmds:
            cfg.list_keywords = cmds["list"]
        if "help" in cmds:
            cfg.help_keywords = cmds["help"]

    # Override from env vars
    cfg.signal_api_url = os.environ.get("SIGNAL_API_URL", cfg.signal_api_url)
    cfg.signal_account = os.environ.get("SIGNAL_ACCOUNT", cfg.signal_account)
    cfg.signal_recipient = os.environ.get("SIGNAL_RECIPIENT", cfg.signal_recipient)
    cfg.bearer_token = os.environ.get("API_BEARER_TOKEN", cfg.bearer_token)
    cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
    cfg.tls_cert = os.environ.get("TLS_CERT_PATH", cfg.tls_cert)
    cfg.tls_key = os.environ.get("TLS_KEY_PATH", cfg.tls_key)

    return cfg
