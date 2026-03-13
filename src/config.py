"""Configuration loader.

Loads config.yaml for escalation patterns and .env for secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class EscalationStage:
    """A single stage in the escalation pattern."""

    offset_hours: float
    interval_min: Optional[int]  # None = single ping
    target: str = "self"  # "self" or "escalate"
    message: str = ""


@dataclass
class EscalationOverflow:
    """Overflow escalation after N minutes with no ack."""

    after_min: int
    interval_min: int
    target: str = "escalate"
    message: str = ""


@dataclass
class EscalationProfile:
    """Full escalation profile configuration."""

    stages: list[EscalationStage]
    post_start_interval_min: int = 2
    post_start_target: str = "self"
    post_start_message: str = ""
    overflow: Optional[EscalationOverflow] = None
    timeout_after_min: Optional[int] = 90  # None = never timeout


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
    api_port: int = 8000

    # Database
    db_path: str = ":memory:"

    # Timezone
    timezone: str = "Europe/London"

    # Escalation profiles
    escalation_profiles: dict[str, EscalationProfile] = field(default_factory=dict)

    # Scheduler
    check_interval_sec: int = 60

    # Public base URL for one-time ack token links (REQ-3, E-11)
    # e.g. "https://klaxxon.example.com" — set via KLAXXON_BASE_URL env var.
    # Trailing slash is stripped during load_config.
    base_url: Optional[str] = None

    # Signal commands
    ack_keywords: list[str] = field(default_factory=lambda: ["ack", "joining"])
    skip_keywords: list[str] = field(default_factory=lambda: ["skip"])
    list_keywords: list[str] = field(default_factory=lambda: ["list", "meetings"])
    help_keywords: list[str] = field(default_factory=lambda: ["help"])

    # Housekeeping: age-out of terminal reminders
    retention_days: int = (
        30  # days to keep terminal reminders (0 = disable auto-cleanup)
    )
    cleanup_interval_hours: int = 1  # how often automatic cleanup runs (hours)


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

        # Parse escalation profiles
        profiles_data = data.get("escalation_profiles", {})
        for profile_name, profile_data in profiles_data.items():
            stages = []
            for s in profile_data.get("stages", []):
                stages.append(
                    EscalationStage(
                        offset_hours=s["offset_hours"],
                        interval_min=s.get("interval_min"),
                        target=s.get("target", "self"),
                        message=s.get("message", ""),
                    )
                )

            overflow = None
            if "overflow" in profile_data:
                ov = profile_data["overflow"]
                overflow = EscalationOverflow(
                    after_min=ov["after_min"],
                    interval_min=ov["interval_min"],
                    target=ov.get("target", "escalate"),
                    message=ov.get("message", ""),
                )

            cfg.escalation_profiles[profile_name] = EscalationProfile(
                stages=stages,
                post_start_interval_min=profile_data.get("post_start_interval_min", 2),
                post_start_target=profile_data.get("post_start_target", "self"),
                post_start_message=profile_data.get("post_start_message", ""),
                overflow=overflow,
                timeout_after_min=profile_data.get("timeout_after_min", 90),
            )

        # Scheduler
        sched = data.get("scheduler", {})
        cfg.check_interval_sec = sched.get("check_interval_sec", cfg.check_interval_sec)

        # Housekeeping
        hk = data.get("housekeeping", {})
        cfg.retention_days = hk.get("retention_days", cfg.retention_days)
        cfg.cleanup_interval_hours = hk.get(
            "cleanup_interval_hours", cfg.cleanup_interval_hours
        )

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

    # Public base URL for ack token links (E-11: strip trailing slash)
    raw_base_url = os.environ.get("KLAXXON_BASE_URL", "").strip()
    if raw_base_url:
        cfg.base_url = raw_base_url.rstrip("/")

    # Housekeeping: retention_days env var override (0 = disable auto-cleanup)
    raw_retention = os.environ.get("KLAXXON_RETENTION_DAYS", "").strip()
    if raw_retention:
        try:
            cfg.retention_days = int(raw_retention)
        except ValueError:
            pass  # Ignore invalid values; keep whatever was in config.yaml or default

    return cfg
