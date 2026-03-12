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
from .api.ack_routes import router as ack_router
from .api.ack_routes import set_dependencies as ack_set_dependencies
from .api.auth import register_token
from .config import AppConfig, load_config
from .repository.sqlite import SqliteReminderRepository
from .repository.schedule_sqlite import SqliteScheduleRepository
from .services.ack_token_service import AckTokenService
from .services.housekeeping_service import HousekeepingService
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
_housekeeping: HousekeepingService | None = None
_last_cleanup: float = 0.0  # monotonic time of last cleanup


async def _scheduler_loop() -> None:
    """Background loop: runs reminder engine, schedule spawner, and signal handler."""
    global _last_cleanup

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

        # Throttled housekeeping cleanup (REQ-6, AC-8, AC-9)
        if _housekeeping is not None and _config.retention_days > 0:
            now_mono = asyncio.get_event_loop().time()
            if now_mono - _last_cleanup >= _config.cleanup_interval_hours * 3600:
                try:
                    _housekeeping.cleanup()
                    _last_cleanup = now_mono
                except Exception:
                    logger.exception("Housekeeping cleanup error")

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    global _reminder_engine, _schedule_service, _signal_handler, _config, _housekeeping

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

    # Ack token service — requires the shared reminder_repo (same DB connection)
    ack_token_service = AckTokenService(
        repository=reminder_repo,
        base_url=_config.base_url,
    )
    if _config.base_url:
        logger.info("Ack token URL base: %s", _config.base_url)
    else:
        logger.info("KLAXXON_BASE_URL not set — ack URLs disabled")

    _reminder_engine = ReminderEngine(
        service=service,
        repository=reminder_repo,
        sender=signal_client,
        recipient=_config.signal_recipient,
        escalation_profiles=_config.escalation_profiles,
        ack_token_service=ack_token_service,
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

    # Housekeeping service (REQ-6, REQ-9)
    _housekeeping = HousekeepingService(
        repository=reminder_repo,
        retention_days=_config.retention_days,
    )
    if _config.retention_days > 0:
        logger.info(
            "Housekeeping: retention_days=%d, cleanup_interval_hours=%d",
            _config.retention_days,
            _config.cleanup_interval_hours,
        )
    else:
        logger.info("Housekeeping: automatic cleanup disabled (retention_days=0)")

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
        housekeeping_service=_housekeeping,
    )

    # Set ack route dependencies (public router, no auth)
    ack_set_dependencies(
        service=service,
        ack_token_service=ack_token_service,
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
app.include_router(ack_router)  # Public ack endpoint — no auth required (REQ-5, REQ-14)

# Serve SPA static files (web/ directory alongside src/)
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="static")
