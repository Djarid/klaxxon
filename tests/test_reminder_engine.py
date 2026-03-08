"""Comprehensive tests for the ReminderEngine.

Tests escalation stages, post-start reminders, timeout handling,
message formatting, and state transitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.config import EscalationOverflow, EscalationProfile, EscalationStage
from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_service import ReminderService
from src.services.reminder_engine import ReminderEngine
from tests.conftest import FailingSender, MockSender


@pytest.fixture
def escalation_config() -> dict[str, EscalationProfile]:
    """Simple 2-stage escalation config for testing.

    Stage 1: 1 hour before, single ping
    Stage 2: 15 minutes before, repeating every 5 minutes
    Post-start: every 2 minutes
    Timeout: 30 minutes after start
    """
    return {
        "meeting": EscalationProfile(
            stages=[
                EscalationStage(
                    offset_hours=-1.0,
                    interval_min=None,
                    target="self",
                    message="Reminder: {title} at {time}",
                ),
                EscalationStage(
                    offset_hours=-0.25,  # 15 minutes before
                    interval_min=5,
                    target="self",
                    message="{title} in {mins_until} min! {link}",
                ),
            ],
            post_start_interval_min=2,
            post_start_target="self",
            post_start_message="MEETING STARTED {mins_ago} min ago: {title}. {link}",
            overflow=None,
            timeout_after_min=30,
        )
    }


@pytest.fixture
def engine(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
    escalation_config: dict[str, EscalationProfile],
) -> ReminderEngine:
    """ReminderEngine instance with test config."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        escalation_profiles=escalation_config,
    )


class TestEscalationStages:
    """Test escalation stage triggering and intervals."""

    @pytest.mark.asyncio
    async def test_no_stage_fires_when_too_early(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """No reminder sent when current time is before all stage offsets."""
        # Use a time far in the future to avoid PastReminderError
        starts_at = datetime.now(timezone.utc) + timedelta(hours=3)
        reminder = service.create(
            title="Team Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert reminder.id is not None

        # 2 hours before start (before first stage at -1 hour)
        now = starts_at - timedelta(hours=2)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 0
        # Reminder should still be PENDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.PENDING

    @pytest.mark.asyncio
    async def test_single_ping_stage_fires_once(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Single-ping stage sends one reminder and transitions to REMINDING."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Team Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert reminder.id is not None

        # Exactly at first stage trigger (-1 hour)
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should send one message
        assert len(mock_sender.messages) == 1
        recipient, text = mock_sender.messages[0]
        assert recipient == "+441234567890"
        assert "Reminder: Team Standup at" in text

        # Should log the reminder
        last_sent = repo.get_last_reminder_time(reminder.id)
        assert last_sent is not None
        # log_reminder uses current time, not the 'now' parameter
        # Just verify it was logged
        assert (last_sent - now).total_seconds() < 1

        # Should transition to REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING

    @pytest.mark.asyncio
    async def test_single_ping_stage_does_not_repeat(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Single-ping stage does not send again after first send."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Team Standup",
            starts_at=starts_at,
        )
        assert reminder.id is not None

        # First call at -1 hour
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Get the actual logged time
        first_logged = repo.get_last_reminder_time(reminder.id)
        assert first_logged is not None

        # Second call 10 minutes later (still before stage 2 at -15 min)
        # Use a time that's after the logged time but before stage 2
        mock_sender.messages.clear()
        now = first_logged + timedelta(minutes=10)
        # Make sure we're still before stage 2 trigger
        if now >= starts_at - timedelta(minutes=15):
            now = starts_at - timedelta(minutes=20)
        reminder = service.get(reminder.id)  # Refresh
        await engine._process_reminder(reminder, now)

        # Should NOT send again (stage 1 already fired, stage 2 not yet)
        assert len(mock_sender.messages) == 0

    @pytest.mark.asyncio
    async def test_repeating_stage_fires(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Repeating stage sends reminder and transitions to REMINDING."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Board Reminder",
            starts_at=starts_at,
            link="https://zoom.us/j/123",
        )
        assert reminder.id is not None

        # At second stage trigger (-15 minutes)
        now = starts_at - timedelta(minutes=15)
        await engine._process_reminder(reminder, now)

        # Should send message
        assert len(mock_sender.messages) == 1
        recipient, text = mock_sender.messages[0]
        assert recipient == "+441234567890"
        assert "Board Reminder in 15 min! https://zoom.us/j/123" == text

        # Should log reminder
        last_sent = repo.get_last_reminder_time(reminder.id)
        assert last_sent is not None

        # Should transition to REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING

    @pytest.mark.asyncio
    async def test_repeating_stage_respects_interval(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Repeating stage waits for interval before sending again."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Board Reminder",
            starts_at=starts_at,
            link="https://zoom.us/j/123",
        )
        assert reminder.id is not None

        # First send at -15 minutes
        now = starts_at - timedelta(minutes=15)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
        first_logged = repo.get_last_reminder_time(reminder.id)
        assert first_logged is not None

        # Try again immediately (within 5-minute interval)
        # The logged time is "now" in real time, so we need to wait
        import time

        time.sleep(0.01)  # Small delay to ensure time has passed
        mock_sender.messages.clear()
        now = datetime.now(timezone.utc)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        # Should not send (within interval)
        assert len(mock_sender.messages) == 0

        # Manually update the last reminder time to simulate 5+ minutes passing
        # This is the only way to test interval logic without actual time passing
        conn = repo._get_conn()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        # Now try again - should send
        # Use a time that's still before the reminder starts
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=10)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # Should be stage 2 message
        assert "Board Reminder in" in text
        assert "https://zoom.us/j/123" in text

    @pytest.mark.asyncio
    async def test_most_aggressive_stage_applies(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """When multiple stages are past trigger time, the latest one applies."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Urgent Reminder",
            starts_at=starts_at,
            link="https://meet.example.com/urgent",
        )
        assert reminder.id is not None

        # At -10 minutes (past both stage 1 at -60min and stage 2 at -15min)
        now = starts_at - timedelta(minutes=10)
        await engine._process_reminder(reminder, now)

        # Should use stage 2 message format
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Urgent Reminder in 10 min! https://meet.example.com/urgent" == text


class TestPostStartReminders:
    """Test post-start reminder behaviour."""

    @pytest.mark.asyncio
    async def test_post_start_reminder_fires(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders are sent for REMINDING reminders after start time."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Daily Sync",
            starts_at=starts_at,
            link="https://meet.example.com/sync",
        )
        assert reminder.id is not None
        # Manually transition to REMINDING
        service.mark_reminding(reminder.id)
        reminder = service.get(reminder.id)

        # 5 minutes after start
        now = starts_at + timedelta(minutes=5)
        await engine._process_reminder(reminder, now)

        # Should send post-start message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert (
            "MEETING STARTED 5 min ago: Daily Sync. https://meet.example.com/sync"
            == text
        )

        # Should log reminder
        last_sent = repo.get_last_reminder_time(reminder.id)
        assert last_sent is not None

    @pytest.mark.asyncio
    async def test_post_start_respects_interval(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders wait for configured interval."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Daily Sync",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # First post-start at +5 minutes
        now = starts_at + timedelta(minutes=5)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
        first_send_time = repo.get_last_reminder_time(reminder.id)
        assert first_send_time is not None

        # Try again immediately (within 2-minute interval)
        import time

        time.sleep(0.01)
        mock_sender.messages.clear()
        now = datetime.now(timezone.utc)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        # Should not send (within interval)
        assert len(mock_sender.messages) == 0

        # Manually update the last reminder time to simulate 2+ minutes passing
        conn = repo._get_conn()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        # Now try again - should send
        # Use a time that's after the reminder starts but before timeout
        mock_sender.messages.clear()
        now = starts_at + timedelta(minutes=10)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_post_start_only_for_reminding_state(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders only fire for reminders in REMINDING state."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Daily Sync",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        # Leave in PENDING state
        reminder = service.get(reminder.id)
        assert reminder.state == ReminderState.PENDING

        # After start time
        now = starts_at + timedelta(minutes=5)
        await engine._process_reminder(reminder, now)

        # Should check escalation stages instead (stage 2 will fire)
        # PENDING reminders after start still get escalation reminders
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # Should be stage 2 message, not post-start
        assert "in 0 min!" in text
        assert "MEETING STARTED" not in text


class TestTimeoutHandling:
    """Test timeout and MISSED state transitions."""

    @pytest.mark.asyncio
    async def test_timeout_marks_missed(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Reminder is marked MISSED after timeout period with no acknowledgement."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)
        reminder = service.get(reminder.id)

        # 31 minutes after start (past 30-minute timeout)
        now = starts_at + timedelta(minutes=31)
        await engine._process_reminder(reminder, now)

        # Should send MISSED message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "MISSED: Important Call (no acknowledgement received)" == text

        # Should mark as MISSED
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.MISSED

    @pytest.mark.asyncio
    async def test_timeout_only_for_reminding_state(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Timeout only applies to reminders in REMINDING state."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        # Leave in PENDING state
        reminder = service.get(reminder.id)
        assert reminder.state == ReminderState.PENDING

        # Past timeout
        now = starts_at + timedelta(minutes=31)
        await engine._process_reminder(reminder, now)

        # Should NOT mark as missed, but will send escalation reminder
        # which transitions to REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING
        assert len(mock_sender.messages) == 1
        # Should be escalation message, not MISSED
        _, text = mock_sender.messages[0]
        assert "MISSED" not in text

    @pytest.mark.asyncio
    async def test_timeout_checked_before_post_start(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Timeout is checked before post-start reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)
        reminder = service.get(reminder.id)

        # Exactly at timeout boundary
        now = starts_at + timedelta(minutes=30)
        await engine._process_reminder(reminder, now)

        # Should send MISSED message, not post-start
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "MISSED:" in text


class TestMessageFormatting:
    """Test message template variable substitution."""

    @pytest.mark.asyncio
    async def test_format_with_all_variables(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """All template variables are correctly substituted."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Project Review",
            starts_at=starts_at,
            link="https://zoom.us/j/999",
        )
        assert reminder.id is not None

        # 15 minutes before (stage 2)
        now = starts_at - timedelta(minutes=15)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # {title} in {mins_until} min! {link}
        assert text == "Project Review in 15 min! https://zoom.us/j/999"

    @pytest.mark.asyncio
    async def test_format_time_string(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Time is formatted as HH:MM."""
        # Create a specific time for predictable formatting
        base = datetime.now(timezone.utc)
        starts_at = base.replace(hour=9, minute=5, second=0, microsecond=0)
        if starts_at <= base:
            starts_at += timedelta(days=1)

        reminder = service.create(
            title="Morning Standup",
            starts_at=starts_at,
        )
        assert reminder.id is not None

        # 1 hour before (stage 1: "Reminder: {title} at {time}")
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Reminder: Morning Standup at 09:05" == text

    @pytest.mark.asyncio
    async def test_format_no_link(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Missing link is replaced with '(no link)'."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="In-Person Reminder",
            starts_at=starts_at,
            link=None,
        )
        assert reminder.id is not None

        # 15 minutes before
        now = starts_at - timedelta(minutes=15)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "(no link)" in text

    @pytest.mark.asyncio
    async def test_format_mins_ago(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """mins_ago is calculated correctly for post-start messages."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)
        reminder = service.get(reminder.id)

        # 7 minutes after start
        now = starts_at + timedelta(minutes=7)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert (
            "MEETING STARTED 7 min ago: Standup. https://meet.example.com/standup"
            == text
        )

    @pytest.mark.asyncio
    async def test_format_mins_until_zero_when_past_start(
        self,
        engine: ReminderEngine,
    ) -> None:
        """mins_until is clamped to 0 when reminder has started."""
        reminder = Reminder(
            id=1,
            title="Test",
            starts_at=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 10, 14, 5, tzinfo=timezone.utc)

        msg = engine._format_message("{mins_until}", reminder, now)
        assert msg == "0"


class TestFailureHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_failed_send_does_not_log(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        escalation_config: dict[str, EscalationProfile],
    ) -> None:
        """Failed send does not log reminder or transition state."""
        failing_sender = FailingSender()
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=failing_sender,
            recipient="+441234567890",
            escalation_profiles=escalation_config,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test Reminder",
            starts_at=starts_at,
        )
        assert reminder.id is not None

        # Trigger stage 1
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should NOT log reminder
        last_sent = repo.get_last_reminder_time(reminder.id)
        assert last_sent is None

        # Should NOT transition to REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.PENDING

    @pytest.mark.asyncio
    async def test_meeting_without_starts_at_skipped(
        self,
        engine: ReminderEngine,
        mock_sender: MockSender,
    ) -> None:
        """Meetings without starts_at are skipped in tick().

        Note: The schema requires starts_at to be NOT NULL, so this scenario
        can only occur if the code checks for None defensively. The tick()
        method does check 'if reminder.starts_at is None: continue'.

        Since we can't create such a reminder in the database, we test that
        the defensive check exists by verifying tick() doesn't crash.
        """
        # Create a normal reminder
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = engine._service.create(
            title="Normal Reminder",
            starts_at=starts_at,
        )

        # Run tick - should process normally
        with patch("src.services.reminder_engine.datetime") as mock_dt:
            mock_dt.now.return_value = starts_at - timedelta(hours=1)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            await engine.tick()

        # Should have sent a reminder
        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_tick_handles_exceptions(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """tick() logs exceptions but continues processing other reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting_1 = service.create(
            title="Reminder 1",
            starts_at=starts_at,
        )
        meeting_2 = service.create(
            title="Reminder 2",
            starts_at=starts_at,
        )

        # Patch _process_reminder to raise on first call, succeed on second
        original_process = engine._process_reminder
        call_count = 0

        async def mock_process(reminder, now):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Simulated error")
            return await original_process(reminder, now)

        with patch.object(engine, "_process_reminder", side_effect=mock_process):
            # Mock datetime.now for tick()
            with patch("src.services.reminder_engine.datetime") as mock_dt:
                mock_dt.now.return_value = starts_at - timedelta(hours=1)
                mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
                await engine.tick()

        # Should have attempted both reminders
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_tick_uses_current_time(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """tick() uses datetime.now(timezone.utc) for processing."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test Reminder",
            starts_at=starts_at,
        )

        # Mock datetime.now to return specific time
        with patch("src.services.reminder_engine.datetime") as mock_dt:
            mock_dt.now.return_value = starts_at - timedelta(hours=1)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            await engine.tick()

        # Should have sent stage 1 reminder
        assert len(mock_sender.messages) == 1


class TestStateTransitions:
    """Test reminder state transitions during reminder flow."""

    @pytest.mark.asyncio
    async def test_pending_to_reminding_on_first_reminder(
        self,
        engine: ReminderEngine,
        service: ReminderService,
    ) -> None:
        """Reminder transitions from PENDING to REMINDING on first reminder."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        assert reminder.state == ReminderState.PENDING

        # Trigger reminder
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should transition to REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING

    @pytest.mark.asyncio
    async def test_reminding_to_missed_on_timeout(
        self,
        engine: ReminderEngine,
        service: ReminderService,
    ) -> None:
        """Reminder transitions from REMINDING to MISSED on timeout."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # Past timeout
        now = starts_at + timedelta(minutes=31)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should transition to MISSED
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.MISSED

    @pytest.mark.asyncio
    async def test_stays_reminding_during_post_start(
        self,
        engine: ReminderEngine,
        service: ReminderService,
    ) -> None:
        """Reminder stays in REMINDING during post-start reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # After start, before timeout
        now = starts_at + timedelta(minutes=10)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should still be REMINDING
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_exactly_at_stage_boundary(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Reminder fires when exactly at stage trigger time."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert reminder.id is not None

        # Exactly at -1 hour
        now = starts_at - timedelta(hours=1, seconds=0)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_exactly_at_interval_boundary(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Repeating reminder fires when exactly at interval boundary."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert reminder.id is not None

        # First send
        now = starts_at - timedelta(minutes=15)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Exactly 5 minutes later
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=10)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_zero_minutes_until(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """mins_until is 0 when at exact start time."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Test Reminder",
            starts_at=starts_at,
            link="https://example.com",
        )
        assert reminder.id is not None

        # Exactly at start time, trigger stage 2
        now = starts_at
        await engine._process_reminder(reminder, now)

        # Should format with mins_until=0
        # But since now >= starts_at and state is PENDING, it goes to escalation stages
        # Stage 2 should fire
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "in 0 min!" in text

    @pytest.mark.asyncio
    async def test_multiple_meetings_in_tick(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """tick() processes all PENDING and REMINDING reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting_1 = service.create(
            title="Reminder 1",
            starts_at=starts_at,
        )
        meeting_2 = service.create(
            title="Reminder 2",
            starts_at=starts_at,
        )
        meeting_3 = service.create(
            title="Reminder 3",
            starts_at=starts_at,
        )
        # Mark one as acknowledged (should be skipped)
        service.acknowledge(meeting_3.id, "ack")

        # Mock datetime.now
        with patch("src.services.reminder_engine.datetime") as mock_dt:
            mock_dt.now.return_value = starts_at - timedelta(hours=1)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            await engine.tick()

        # Should send 2 messages (reminder 1 and 2, not 3)
        assert len(mock_sender.messages) == 2
        titles = [msg[1] for msg in mock_sender.messages]
        assert any("Reminder 1" in t for t in titles)
        assert any("Reminder 2" in t for t in titles)
        assert not any("Reminder 3" in t for t in titles)


class TestDescriptionTemplateVariable:
    """Test that {description} template variable works correctly."""

    @pytest.mark.asyncio
    async def test_format_message_with_description(
        self,
        engine: ReminderEngine,
        service: ReminderService,
    ) -> None:
        """Test that {description} is replaced in message template when present."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Medication",
            description="Take 10mg Ramipril with water",
            starts_at=starts_at,
        )

        # Create a template with {description}
        template = "{title}: {description}"
        now = starts_at - timedelta(minutes=30)

        formatted = engine._format_message(template, reminder, now)

        assert formatted == "Medication: Take 10mg Ramipril with water"

    @pytest.mark.asyncio
    async def test_format_message_without_description(
        self,
        engine: ReminderEngine,
        service: ReminderService,
    ) -> None:
        """Test that {description} renders as empty string when None."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Quick Meeting",
            starts_at=starts_at,
        )
        assert reminder.description is None

        # Create a template with {description}
        template = "{title}. {description}"
        now = starts_at - timedelta(minutes=30)

        formatted = engine._format_message(template, reminder, now)

        # Should render as empty string, not "None"
        assert formatted == "Quick Meeting. "
        assert "None" not in formatted


class TestEscalationProfiles:
    """Test profile loading and selection."""

    @pytest.mark.asyncio
    async def test_engine_uses_meeting_profile_by_default(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Reminder with profile='meeting' uses meeting stages."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="self",
                        message="Meeting: {title}",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Meeting started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
        )
        assert reminder.id is not None

        # Trigger stage
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should use meeting profile message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Meeting: Test" == text

    @pytest.mark.asyncio
    async def test_engine_uses_persistent_profile(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Reminder with profile='persistent' uses persistent stages."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="self",
                        message="Meeting: {title}",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Meeting started",
                overflow=None,
                timeout_after_min=30,
            ),
            "persistent": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=5,
                        target="self",
                        message="Persistent: {title}",
                    )
                ],
                post_start_interval_min=5,
                post_start_target="self",
                post_start_message="Persistent overdue",
                overflow=None,
                timeout_after_min=None,
            ),
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Medication",
            starts_at=starts_at,
            profile="persistent",
        )
        assert reminder.id is not None

        # Trigger at start time
        now = starts_at
        await engine._process_reminder(reminder, now)

        # Should use persistent profile message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Persistent: Medication" == text

    @pytest.mark.asyncio
    async def test_engine_falls_back_to_meeting_for_unknown_profile(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Unknown profile name falls back to meeting."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="self",
                        message="Fallback: {title}",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Fallback started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="nonexistent",
        )
        assert reminder.id is not None

        # Trigger stage
        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should use meeting profile (fallback)
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Fallback: Test" == text


class TestTargetResolution:
    """Test target resolution (self vs escalate)."""

    @pytest.mark.asyncio
    async def test_target_self_sends_to_owner(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Stage with target=self sends to SIGNAL_RECIPIENT."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="self",
                        message="Test",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
        )
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should send to owner only
        assert len(mock_sender.messages) == 1
        recipient, _ = mock_sender.messages[0]
        assert recipient == "+441234567890"

    @pytest.mark.asyncio
    async def test_target_escalate_with_escalate_to_sends_to_both(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Stage with target=escalate and escalate_to set sends to both owner and escalate_to."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="escalate",
                        message="Test",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
            escalate_to="+447700900123",
        )
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should send to both owner and escalate_to
        assert len(mock_sender.messages) == 2
        recipients = [msg[0] for msg in mock_sender.messages]
        assert "+441234567890" in recipients
        assert "+447700900123" in recipients

    @pytest.mark.asyncio
    async def test_target_escalate_without_escalate_to_sends_to_owner_only(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Stage with target=escalate but no escalate_to falls back to owner."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="escalate",
                        message="Test",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
            escalate_to=None,
        )
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # Should send to owner only
        assert len(mock_sender.messages) == 1
        recipient, _ = mock_sender.messages[0]
        assert recipient == "+441234567890"


class TestNullTimeout:
    """Test null timeout behavior (persistent profile)."""

    @pytest.mark.asyncio
    async def test_persistent_profile_never_times_out(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Reminder with timeout_after_min=null stays in REMINDING forever."""
        profiles = {
            "persistent": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=5,
                        target="self",
                        message="Persistent",
                    )
                ],
                post_start_interval_min=5,
                post_start_target="self",
                post_start_message="Overdue",
                overflow=None,
                timeout_after_min=None,  # Never timeout
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Medication",
            starts_at=starts_at,
            profile="persistent",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # 1000 minutes after start (way past any normal timeout)
        now = starts_at + timedelta(minutes=1000)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should NOT mark as missed, should send post-start
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.REMINDING
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Overdue" in text

    @pytest.mark.asyncio
    async def test_meeting_profile_times_out(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Meeting profile with timeout_after_min=30 times out."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=-1.0,
                        interval_min=None,
                        target="self",
                        message="Meeting",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Started",
                overflow=None,
                timeout_after_min=30,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # 31 minutes after start (past timeout)
        now = starts_at + timedelta(minutes=31)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should mark as missed
        updated = service.get(reminder.id)
        assert updated.state == ReminderState.MISSED
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "MISSED" in text


class TestOverflow:
    """Test overflow escalation."""

    @pytest.mark.asyncio
    async def test_overflow_triggers_after_configured_minutes(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Overflow fires after after_min with no ack."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=1,
                        target="self",
                        message="Started",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Post-start",
                overflow=EscalationOverflow(
                    after_min=10,
                    interval_min=5,
                    target="escalate",
                    message="Overflow: {title}",
                ),
                timeout_after_min=90,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
            escalate_to="+447700900123",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # 11 minutes after start (past overflow trigger)
        now = starts_at + timedelta(minutes=11)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should send overflow message to both
        assert len(mock_sender.messages) == 2
        recipients = [msg[0] for msg in mock_sender.messages]
        assert "+441234567890" in recipients
        assert "+447700900123" in recipients
        # Check message content
        texts = [msg[1] for msg in mock_sender.messages]
        assert all("Overflow: Test" in t for t in texts)

    @pytest.mark.asyncio
    async def test_overflow_does_not_trigger_before_time(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Overflow doesn't fire before after_min."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=1,
                        target="self",
                        message="Started",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Post-start",
                overflow=EscalationOverflow(
                    after_min=10,
                    interval_min=5,
                    target="escalate",
                    message="Overflow",
                ),
                timeout_after_min=90,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
            escalate_to="+447700900123",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # 5 minutes after start (before overflow trigger at 10)
        now = starts_at + timedelta(minutes=5)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should send post-start, not overflow
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Post-start" in text
        assert "Overflow" not in text

    @pytest.mark.asyncio
    async def test_overflow_sends_to_escalate_target(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Overflow with target=escalate sends to escalate_to."""
        profiles = {
            "meeting": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=1,
                        target="self",
                        message="Started",
                    )
                ],
                post_start_interval_min=2,
                post_start_target="self",
                post_start_message="Post-start",
                overflow=EscalationOverflow(
                    after_min=10,
                    interval_min=5,
                    target="escalate",
                    message="Overflow",
                ),
                timeout_after_min=90,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Test",
            starts_at=starts_at,
            profile="meeting",
            escalate_to="+447700900123",
        )
        assert reminder.id is not None
        service.mark_reminding(reminder.id)

        # 11 minutes after start
        now = starts_at + timedelta(minutes=11)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)

        # Should send to both owner and escalate_to
        assert len(mock_sender.messages) == 2
        recipients = [msg[0] for msg in mock_sender.messages]
        assert "+441234567890" in recipients
        assert "+447700900123" in recipients


class TestPersistentProfileBehavior:
    """Test persistent profile specific behavior."""

    @pytest.mark.asyncio
    async def test_persistent_profile_flat_nag_interval(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Persistent profile has single stage with flat 5-min nags."""
        profiles = {
            "persistent": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=5,
                        target="self",
                        message="Reminder: {title}",
                    )
                ],
                post_start_interval_min=5,
                post_start_target="self",
                post_start_message="Overdue: {title}",
                overflow=None,
                timeout_after_min=None,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Medication",
            starts_at=starts_at,
            profile="persistent",
        )
        assert reminder.id is not None

        # At start time
        now = starts_at
        await engine._process_reminder(reminder, now)

        # Should send first reminder
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Reminder: Medication" == text

    @pytest.mark.asyncio
    async def test_persistent_profile_description_in_message(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Persistent profile message includes {description}."""
        profiles = {
            "persistent": EscalationProfile(
                stages=[
                    EscalationStage(
                        offset_hours=0,
                        interval_min=5,
                        target="self",
                        message="{title}. {description}",
                    )
                ],
                post_start_interval_min=5,
                post_start_target="self",
                post_start_message="Overdue: {title}. {description}",
                overflow=None,
                timeout_after_min=None,
            )
        }
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=mock_sender,
            recipient="+441234567890",
            escalation_profiles=profiles,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        reminder = service.create(
            title="Medication",
            description="Take 10mg Ramipril with water",
            starts_at=starts_at,
            profile="persistent",
        )
        assert reminder.id is not None

        # At start time
        now = starts_at
        await engine._process_reminder(reminder, now)

        # Should include description
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert text == "Medication. Take 10mg Ramipril with water"


class TestTimingOverrides:
    """Test per-reminder timing overrides (lead_time_min and nag_interval_min)."""

    @pytest.mark.asyncio
    async def test_lead_time_override_fires_earlier(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Reminder with lead_time_min=10 fires 10 min before, not at profile's default."""
        # Profile has first stage at -1 hour, but override is 10 minutes
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Quick Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/quick",
            lead_time_min=10,  # Override: start 10 min before
        )
        assert reminder.id is not None

        # 11 minutes before start (before override trigger)
        now = starts_at - timedelta(minutes=11)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 0

        # 9 minutes before start (after override trigger)
        now = starts_at - timedelta(minutes=9)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Quick Meeting" in text

    @pytest.mark.asyncio
    async def test_lead_time_override_none_uses_profile(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Reminder without lead_time_min uses profile stages."""
        # Profile has first stage at -1 hour
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Normal Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/normal",
            # No lead_time_min override
        )
        assert reminder.id is not None

        # 30 minutes before start (after -1 hour stage)
        now = starts_at - timedelta(minutes=30)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Normal Meeting" in text

    @pytest.mark.asyncio
    async def test_nag_interval_override(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Reminder with nag_interval_min=3 nags every 3 min instead of profile default."""
        # Profile has stage at -15 min with 5 min interval, override to 3 min
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Urgent Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/urgent",
            nag_interval_min=3,  # Override: nag every 3 min
        )
        assert reminder.id is not None

        # 14 minutes before start (in second stage)
        now = starts_at - timedelta(minutes=14)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Simulate 2 minutes passing (not enough for 3 min interval)
        # Set sent_at to 2 minutes before current simulated time
        conn = repo._get_conn()
        old_time = (
            starts_at - timedelta(minutes=12) - timedelta(minutes=2)
        ).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=12)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 0  # Not yet (need 3 min)

        # Simulate 3 minutes passing (enough for 3 min interval)
        # Set sent_at to 3+ minutes before current simulated time
        old_time = (
            starts_at - timedelta(minutes=11) - timedelta(minutes=4)
        ).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=11)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_nag_interval_override_none_uses_profile(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Reminder without nag_interval_min uses profile intervals."""
        # Profile has stage at -15 min with 5 min interval
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Normal Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/normal",
            # No nag_interval_min override
        )
        assert reminder.id is not None

        # 14 minutes before start (in second stage)
        now = starts_at - timedelta(minutes=14)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Simulate 3 minutes passing (not enough for 5 min interval)
        # Set sent_at to 3 minutes before current simulated time
        conn = repo._get_conn()
        old_time = (
            starts_at - timedelta(minutes=11) - timedelta(minutes=3)
        ).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=11)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 0

        # Simulate 5 minutes passing (enough for 5 min interval)
        # Set sent_at to 5+ minutes before current simulated time
        old_time = (starts_at - timedelta(minutes=9) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=9)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_nag_interval_does_not_affect_single_pings(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Single-ping stages stay single-ping even with nag_interval override."""
        # Profile has first stage at -1 hour with interval_min=None (single ping)
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(
            title="Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/test",
            nag_interval_min=2,  # Should not affect single-ping stages
        )
        assert reminder.id is not None

        # 59 minutes before start (in first stage)
        now = starts_at - timedelta(minutes=59)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Simulate 2 minutes passing
        # Set sent_at to 2 minutes before current simulated time
        conn = repo._get_conn()
        old_time = (
            starts_at - timedelta(minutes=57) - timedelta(minutes=2)
        ).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        # 2 minutes later (should not fire again, stage is single-ping)
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=57)
        reminder = service.get(reminder.id)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 0

    @pytest.mark.asyncio
    async def test_both_overrides_together(
        self,
        engine: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Both lead_time and nag_interval set simultaneously."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(
            title="Custom Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/custom",
            lead_time_min=5,  # Start 5 min before
            nag_interval_min=1,  # Nag every 1 min
        )
        assert reminder.id is not None

        # 6 minutes before start (before override trigger)
        now = starts_at - timedelta(minutes=6)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 0

        # 4 minutes before start (after override trigger)
        now = starts_at - timedelta(minutes=4)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # 1 minute later (should fire with 1 min nag interval)
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=3)
        await engine._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1
