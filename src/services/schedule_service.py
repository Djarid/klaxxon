"""Schedule service: manages recurring reminders.

Spawns reminder instances from schedule templates.
Handles recurrence resolution and timezone conversion.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..models.reminder import Reminder, ReminderState
from ..models.schedule import Schedule
from ..repository.base import ReminderRepository
from ..repository.schedule_sqlite import SqliteScheduleRepository

logger = logging.getLogger(__name__)

# Day abbreviations for recurrence rules
DAY_ABBREVS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class ScheduleValidationError(Exception):
    """Raised when schedule validation fails."""

    pass


class ScheduleNotFoundError(Exception):
    """Raised when a schedule is not found."""

    pass


class ScheduleService:
    """Business logic for schedule operations and reminder spawning."""

    def __init__(
        self,
        schedule_repo: SqliteScheduleRepository,
        reminder_repo: ReminderRepository,
        timezone_name: str = "Europe/London",
    ) -> None:
        self._schedule_repo = schedule_repo
        self._reminder_repo = reminder_repo
        self._timezone = ZoneInfo(timezone_name)

    def create(
        self,
        title: str,
        time_of_day: str,
        recurrence: str,
        description: Optional[str] = None,
        duration_min: int = 0,
        link: Optional[str] = None,
        profile: str = "meeting",
        escalate_to: Optional[str] = None,
        lead_time_min: Optional[int] = None,
        nag_interval_min: Optional[int] = None,
        recurrence_rule: Optional[str] = None,
        source: str = "manual",
    ) -> Schedule:
        """Create a new schedule.

        Validates:
        - time_of_day format (HH:MM)
        - recurrence value (daily, weekly, custom)
        - recurrence_rule required for weekly/custom
        """
        # Validate time_of_day format
        if not re.match(r"^\d{2}:\d{2}$", time_of_day):
            raise ScheduleValidationError(
                f"time_of_day must be in HH:MM format, got: {time_of_day}"
            )

        # Validate recurrence
        if recurrence not in ("daily", "weekly", "custom"):
            raise ScheduleValidationError(
                f"recurrence must be 'daily', 'weekly', or 'custom', got: {recurrence}"
            )

        # Validate recurrence_rule for weekly/custom
        if recurrence in ("weekly", "custom"):
            if not recurrence_rule:
                raise ScheduleValidationError(
                    f"recurrence_rule is required for {recurrence} recurrence"
                )
            # Validate day abbreviations
            days = {d.strip().lower() for d in recurrence_rule.split(",")}
            invalid_days = days - DAY_ABBREVS
            if invalid_days:
                raise ScheduleValidationError(
                    f"Invalid day abbreviations in recurrence_rule: {invalid_days}. "
                    f"Valid: {DAY_ABBREVS}"
                )

        schedule = Schedule(
            title=title,
            description=description,
            time_of_day=time_of_day,
            duration_min=duration_min,
            link=link,
            source=source,
            profile=profile,
            escalate_to=escalate_to,
            lead_time_min=lead_time_min,
            nag_interval_min=nag_interval_min,
            recurrence=recurrence,
            recurrence_rule=recurrence_rule,
            is_active=True,
        )
        return self._schedule_repo.create(schedule)

    def get(self, schedule_id: int) -> Schedule:
        """Get a schedule by id."""
        schedule = self._schedule_repo.get(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"Schedule {schedule_id} not found")
        return schedule

    def list(self, active_only: bool = True) -> list[Schedule]:
        """List schedules, optionally filtered by active status."""
        return self._schedule_repo.list(active_only=active_only)

    def update(self, schedule_id: int, **fields) -> Schedule:
        """Update a schedule's fields."""
        # Validate schedule exists
        schedule = self.get(schedule_id)

        # Validate fields if present
        if "time_of_day" in fields:
            if not re.match(r"^\d{2}:\d{2}$", fields["time_of_day"]):
                raise ScheduleValidationError(
                    f"time_of_day must be in HH:MM format, got: {fields['time_of_day']}"
                )

        if "recurrence" in fields:
            if fields["recurrence"] not in ("daily", "weekly", "custom"):
                raise ScheduleValidationError(
                    f"recurrence must be 'daily', 'weekly', or 'custom', got: {fields['recurrence']}"
                )

        # Update fields
        updated = self._schedule_repo.update_fields(schedule_id, fields)
        if updated is None:
            raise ScheduleNotFoundError(f"Schedule {schedule_id} not found")

        logger.info("Schedule %d updated: %s", schedule_id, list(fields.keys()))
        return updated

    def deactivate(self, schedule_id: int) -> bool:
        """Deactivate a schedule (soft delete)."""
        success = self._schedule_repo.deactivate(schedule_id)
        if success:
            logger.info("Schedule %d deactivated", schedule_id)
        return success

    def spawn_reminders(self) -> list[Reminder]:
        """Spawn reminder instances from active schedules.

        Logic:
        1. Get all active schedules
        2. For each schedule:
           a. If an active (PENDING or REMINDING) instance already exists, skip.
           b. Otherwise, calculate next occurrences within 48-hour window and
              create only the FIRST (earliest) non-duplicate occurrence.
        3. Return list of newly created reminders.

        NEW RULE: At most one active (PENDING/REMINDING) instance per schedule.
        If an active instance already exists, no new instance is created.
        When no active instance exists, only the next (earliest) occurrence is
        materialised — not all occurrences in the 48h window (REQ-7).

        This prevents catch-up flooding when the app was offline: only the
        single next-upcoming occurrence is spawned (REQ-4).

        Handles:
        - Daily, weekly, and custom recurrence patterns
        - Timezone conversion (local time → UTC)
        - DST transitions (via ZoneInfo)
        """
        schedules = self._schedule_repo.list(active_only=True)
        if not schedules:
            return []

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(self._timezone)
        window_end = now_utc + timedelta(hours=48)

        spawned: list[Reminder] = []

        for schedule in schedules:
            # REQ-1 / REQ-2: Skip this schedule if an active instance already exists.
            if self._reminder_repo.has_active_for_schedule(schedule.id):
                logger.debug(
                    "Schedule %d (%s): active instance exists, skipping spawn",
                    schedule.id,
                    schedule.title,
                )
                continue

            occurrences = self._calculate_occurrences(schedule, now_local, window_end)

            # REQ-7: Create only the FIRST (earliest) non-duplicate occurrence.
            for occurrence_utc in occurrences:
                # Secondary dedup guard: same schedule + same time within 1 min
                if self._reminder_exists(schedule.id, occurrence_utc):
                    continue

                # Create reminder
                reminder = Reminder(
                    title=schedule.title,
                    description=schedule.description,
                    starts_at=occurrence_utc,
                    duration_min=schedule.duration_min,
                    link=schedule.link,
                    source=schedule.source,
                    profile=schedule.profile,
                    escalate_to=schedule.escalate_to,
                    lead_time_min=schedule.lead_time_min,
                    nag_interval_min=schedule.nag_interval_min,
                    schedule_id=schedule.id,
                    state=ReminderState.PENDING,
                )
                created = self._reminder_repo.create(reminder)
                spawned.append(created)
                logger.info(
                    "Spawned reminder %d from schedule %d: %s at %s",
                    created.id,
                    schedule.id,
                    schedule.title,
                    occurrence_utc.isoformat(),
                )
                # Stop after the first occurrence — at most one per schedule per spawn.
                break

        if spawned:
            logger.info("Spawned %d reminders from schedules", len(spawned))

        return spawned

    def _calculate_occurrences(
        self,
        schedule: Schedule,
        now_local: datetime,
        window_end_utc: datetime,
    ) -> list[datetime]:
        """Calculate occurrence times within the spawning window.

        Returns list of UTC datetimes.
        """
        occurrences: list[datetime] = []

        # Parse time_of_day
        hour, minute = map(int, schedule.time_of_day.split(":"))

        # Start from today
        current_date = now_local.date()
        end_date = window_end_utc.astimezone(self._timezone).date()

        # Iterate through dates in the window
        while current_date <= end_date:
            # Check if this date matches the recurrence pattern
            if self._matches_recurrence(schedule, current_date):
                # Create local datetime
                local_dt = datetime.combine(
                    current_date,
                    datetime.min.time().replace(hour=hour, minute=minute),
                )
                local_dt = local_dt.replace(tzinfo=self._timezone)

                # Convert to UTC
                utc_dt = local_dt.astimezone(timezone.utc)

                # Only include if within window
                # Note: For meeting profile, we need to spawn 24h in advance
                # So we check if the occurrence is within the 48h window
                if utc_dt <= window_end_utc:
                    occurrences.append(utc_dt)

            current_date += timedelta(days=1)

        return occurrences

    def _matches_recurrence(self, schedule: Schedule, date: datetime.date) -> bool:
        """Check if a date matches the schedule's recurrence pattern."""
        if schedule.recurrence == "daily":
            return True

        if schedule.recurrence in ("weekly", "custom"):
            if not schedule.recurrence_rule:
                return False

            # Get day of week (0=Monday, 6=Sunday)
            weekday = date.weekday()
            day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            day_abbrev = day_names[weekday]

            # Parse recurrence_rule
            allowed_days = {
                d.strip().lower() for d in schedule.recurrence_rule.split(",")
            }
            return day_abbrev in allowed_days

        return False

    def _reminder_exists(self, schedule_id: Optional[int], starts_at: datetime) -> bool:
        """Check if a reminder already exists for this schedule and time.

        Uses 1-minute tolerance to handle minor timing differences.
        """
        if schedule_id is None:
            return False

        # Get all reminders for this schedule
        all_reminders = self._reminder_repo.list_all()
        for reminder in all_reminders:
            if reminder.schedule_id == schedule_id and reminder.starts_at:
                # Check if within 1 minute
                diff = abs((reminder.starts_at - starts_at).total_seconds())
                if diff < 60:
                    return True

        return False
