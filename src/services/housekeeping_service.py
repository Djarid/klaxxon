"""Housekeeping service: age out terminal-state reminders.

Deletes ACKNOWLEDGED, SKIPPED, and MISSED reminders older than a configurable
retention period, and cleans up orphaned ack_tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models.reminder import ReminderState

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Result of a single cleanup run."""

    deleted_acknowledged: int = 0
    deleted_skipped: int = 0
    deleted_missed: int = 0
    deleted_orphan_tokens: int = 0

    @property
    def deleted_reminders(self) -> int:
        """Total terminal reminders deleted (sum of all three states)."""
        return self.deleted_acknowledged + self.deleted_skipped + self.deleted_missed


class HousekeepingService:
    """Service responsible for cleaning up aged-out terminal reminders."""

    # Terminal states that are eligible for cleanup
    _TERMINAL_STATES = [
        ReminderState.ACKNOWLEDGED,
        ReminderState.SKIPPED,
        ReminderState.MISSED,
    ]

    def __init__(
        self,
        repository,
        retention_days: int = 30,
    ) -> None:
        """Initialise housekeeping service.

        Args:
            repository: A repository that implements both ReminderRepository
                and AckTokenRepository ABCs (typically SqliteReminderRepository).
            retention_days: Default number of days to keep terminal reminders.
                Set to 0 to disable automatic cleanup (manual API still works).
        """
        self._repo = repository
        self.retention_days = retention_days

    def cleanup(
        self,
        retention_days: Optional[int] = None,
        dry_run: bool = False,
    ) -> CleanupResult:
        """Delete terminal-state reminders older than retention_days.

        Args:
            retention_days: Override the configured retention period.
                Uses instance default if None.
            dry_run: If True, count but do not delete.

        Returns:
            CleanupResult with counts of deleted (or would-be-deleted) rows.
        """
        effective_days = (
            retention_days if retention_days is not None else self.retention_days
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days)

        # Delete terminal reminders
        counts = self._repo.delete_terminal_reminders(
            cutoff=cutoff,
            states=self._TERMINAL_STATES,
            dry_run=dry_run,
        )

        deleted_acknowledged = counts.get(ReminderState.ACKNOWLEDGED.value, 0)
        deleted_skipped = counts.get(ReminderState.SKIPPED.value, 0)
        deleted_missed = counts.get(ReminderState.MISSED.value, 0)

        # Clean up orphan tokens
        deleted_orphan_tokens = self._repo.delete_orphan_tokens(dry_run=dry_run)

        result = CleanupResult(
            deleted_acknowledged=deleted_acknowledged,
            deleted_skipped=deleted_skipped,
            deleted_missed=deleted_missed,
            deleted_orphan_tokens=deleted_orphan_tokens,
        )

        # REQ-8: Log counts at INFO level
        action = "Would delete" if dry_run else "Housekeeping: deleted"
        logger.info(
            "%s %d terminal reminders (%d acknowledged, %d skipped, %d missed), "
            "%d orphan tokens",
            action,
            result.deleted_reminders,
            result.deleted_acknowledged,
            result.deleted_skipped,
            result.deleted_missed,
            result.deleted_orphan_tokens,
        )

        return result
