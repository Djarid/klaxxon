"""Klaxxon application entry point.

Composition root: wires concrete implementations to abstractions.
Dependency Inversion: this is the ONLY file that imports concretions.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import routes
from .api.auth import register_token
from .config import AppConfig, load_config
from .repository.sqlite import SqliteReminderRepository
from .repository.schedule_sqlite import SqliteScheduleRepository
from .services.reminder_service import ReminderService
from .services.schedule_service import ScheduleService
from .services.notification.signal_client import SignalClient
from .services.reminder_engine import ReminderEngine
from .signal_handler import SignalHandler

logger = logging.getLogger(__name__)

# Module-level references for the background tasks
_reminder_engine: ReminderEngine | None = None
_schedule_service: ScheduleService | None = None
_signal_handler: SignalHandler | None = None
_config: AppConfig | None = None


async def _scheduler_loop() -> None:
    """Background loop: runs reminder engine, schedule spawner, and signal handler."""
    assert _reminder_engine is not None
    assert _schedule_service is not None
    assert _signal_handler is not None
    assert _config is not None

    interval = _config.check_interval_sec
    logger.info("Scheduler started (interval: %ds)", interval)

    while True:
        try:
            await _reminder_engine.tick()
        except Exception:
            logger.exception("Reminder engine error")

        try:
            # Spawn reminders from schedules (synchronous call)
            _schedule_service.spawn_reminders()
        except Exception:
            logger.exception("Schedule spawner error")

        try:
            await _signal_handler.poll()
        except Exception:
            logger.exception("Signal handler error")

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    global _reminder_engine, _schedule_service, _signal_handler, _config

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    _config = load_config(config_path)

    # Wire up concretions (Dependency Inversion: only here)
    reminder_repo = SqliteReminderRepository(_config.db_path)
    schedule_repo = SqliteScheduleRepository(_config.db_path)
    signal_client = SignalClient(
        api_url=_config.signal_api_url,
        account=_config.signal_account,
    )
    service = ReminderService(reminder_repo)

    _schedule_service = ScheduleService(
        schedule_repo=schedule_repo,
        reminder_repo=reminder_repo,
        timezone_name=_config.timezone,
    )

    _reminder_engine = ReminderEngine(
        service=service,
        repository=reminder_repo,
        sender=signal_client,
        recipient=_config.signal_recipient,
        escalation_profiles=_config.escalation_profiles,
    )

    _signal_handler = SignalHandler(
        service=service,
        receiver=signal_client,
        sender=signal_client,
        owner_number=_config.signal_recipient,
        ack_keywords=_config.ack_keywords,
        skip_keywords=_config.skip_keywords,
        list_keywords=_config.list_keywords,
        help_keywords=_config.help_keywords,
    )

    # Register bearer token
    if _config.bearer_token:
        register_token(_config.bearer_token)
    else:
        logger.warning("No API_BEARER_TOKEN configured. API is unprotected.")

    # Set route dependencies
    routes.set_dependencies(
        service=service,
        schedule_service=_schedule_service,
        signal_available_fn=signal_client.is_available,
    )

    # Start background scheduler
    task = asyncio.create_task(_scheduler_loop())
    logger.info("Klaxxon started")

    yield

    # Shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    reminder_repo.close()
    schedule_repo.close()
    logger.info("Klaxxon stopped")


app = FastAPI(
    title="Klaxxon",
    description="ADHD reminder system with escalating Signal notifications",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(routes.router)

# Serve SPA static files (web/ directory alongside src/)
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="static")
