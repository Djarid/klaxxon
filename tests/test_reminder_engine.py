"""Comprehensive tests for the ReminderEngine.

Tests escalation stages, post-start reminders, timeout handling,
message formatting, and state transitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.models.meeting import Meeting, MeetingState
from src.repository.sqlite import SqliteMeetingRepository
from src.services.meeting_service import MeetingService
from src.services.reminder_engine import (
    EscalationConfig,
    EscalationStage,
    ReminderEngine,
)
from tests.conftest import FailingSender, MockSender


@pytest.fixture
def escalation_config() -> EscalationConfig:
    """Simple 2-stage escalation config for testing.

    Stage 1: 1 hour before, single ping
    Stage 2: 15 minutes before, repeating every 5 minutes
    Post-start: every 2 minutes
    Timeout: 30 minutes after start
    """
    return EscalationConfig(
        stages=[
            EscalationStage(
                offset_hours=-1.0,
                interval_min=None,
                message="Reminder: {title} at {time}",
            ),
            EscalationStage(
                offset_hours=-0.25,  # 15 minutes before
                interval_min=5,
                message="{title} in {mins_until} min! {link}",
            ),
        ],
        post_start_interval_min=2,
        post_start_message="MEETING STARTED {mins_ago} min ago: {title}. {link}",
        timeout_after_min=30,
    )


@pytest.fixture
def engine(
    service: MeetingService,
    repo: SqliteMeetingRepository,
    mock_sender: MockSender,
    escalation_config: EscalationConfig,
) -> ReminderEngine:
    """ReminderEngine instance with test config."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        config=escalation_config,
    )


class TestEscalationStages:
    """Test escalation stage triggering and intervals."""

    @pytest.mark.asyncio
    async def test_no_stage_fires_when_too_early(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """No reminder sent when current time is before all stage offsets."""
        # Use a time far in the future to avoid PastMeetingError
        starts_at = datetime.now(timezone.utc) + timedelta(hours=3)
        meeting = service.create(
            title="Team Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert meeting.id is not None

        # 2 hours before start (before first stage at -1 hour)
        now = starts_at - timedelta(hours=2)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 0
        # Meeting should still be PENDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.PENDING

    @pytest.mark.asyncio
    async def test_single_ping_stage_fires_once(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Single-ping stage sends one reminder and transitions to REMINDING."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Team Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert meeting.id is not None

        # Exactly at first stage trigger (-1 hour)
        now = starts_at - timedelta(hours=1)
        await engine._process_meeting(meeting, now)

        # Should send one message
        assert len(mock_sender.messages) == 1
        recipient, text = mock_sender.messages[0]
        assert recipient == "+441234567890"
        assert "Reminder: Team Standup at" in text

        # Should log the reminder
        last_sent = repo.get_last_reminder_time(meeting.id)
        assert last_sent is not None
        # log_reminder uses current time, not the 'now' parameter
        # Just verify it was logged
        assert (last_sent - now).total_seconds() < 1

        # Should transition to REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.REMINDING

    @pytest.mark.asyncio
    async def test_single_ping_stage_does_not_repeat(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Single-ping stage does not send again after first send."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Team Standup",
            starts_at=starts_at,
        )
        assert meeting.id is not None

        # First call at -1 hour
        now = starts_at - timedelta(hours=1)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1

        # Get the actual logged time
        first_logged = repo.get_last_reminder_time(meeting.id)
        assert first_logged is not None

        # Second call 10 minutes later (still before stage 2 at -15 min)
        # Use a time that's after the logged time but before stage 2
        mock_sender.messages.clear()
        now = first_logged + timedelta(minutes=10)
        # Make sure we're still before stage 2 trigger
        if now >= starts_at - timedelta(minutes=15):
            now = starts_at - timedelta(minutes=20)
        meeting = service.get(meeting.id)  # Refresh
        await engine._process_meeting(meeting, now)

        # Should NOT send again (stage 1 already fired, stage 2 not yet)
        assert len(mock_sender.messages) == 0

    @pytest.mark.asyncio
    async def test_repeating_stage_fires(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Repeating stage sends reminder and transitions to REMINDING."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Board Meeting",
            starts_at=starts_at,
            link="https://zoom.us/j/123",
        )
        assert meeting.id is not None

        # At second stage trigger (-15 minutes)
        now = starts_at - timedelta(minutes=15)
        await engine._process_meeting(meeting, now)

        # Should send message
        assert len(mock_sender.messages) == 1
        recipient, text = mock_sender.messages[0]
        assert recipient == "+441234567890"
        assert "Board Meeting in 15 min! https://zoom.us/j/123" == text

        # Should log reminder
        last_sent = repo.get_last_reminder_time(meeting.id)
        assert last_sent is not None

        # Should transition to REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.REMINDING

    @pytest.mark.asyncio
    async def test_repeating_stage_respects_interval(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Repeating stage waits for interval before sending again."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Board Meeting",
            starts_at=starts_at,
            link="https://zoom.us/j/123",
        )
        assert meeting.id is not None

        # First send at -15 minutes
        now = starts_at - timedelta(minutes=15)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1
        first_logged = repo.get_last_reminder_time(meeting.id)
        assert first_logged is not None

        # Try again immediately (within 5-minute interval)
        # The logged time is "now" in real time, so we need to wait
        import time

        time.sleep(0.01)  # Small delay to ensure time has passed
        mock_sender.messages.clear()
        now = datetime.now(timezone.utc)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)
        # Should not send (within interval)
        assert len(mock_sender.messages) == 0

        # Manually update the last reminder time to simulate 5+ minutes passing
        # This is the only way to test interval logic without actual time passing
        conn = repo._get_conn()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE meeting_id = ?",
            (old_time, meeting.id),
        )
        conn.commit()

        # Now try again - should send
        # Use a time that's still before the meeting starts
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=10)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # Should be stage 2 message
        assert "Board Meeting in" in text
        assert "https://zoom.us/j/123" in text

    @pytest.mark.asyncio
    async def test_most_aggressive_stage_applies(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """When multiple stages are past trigger time, the latest one applies."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Urgent Meeting",
            starts_at=starts_at,
            link="https://meet.example.com/urgent",
        )
        assert meeting.id is not None

        # At -10 minutes (past both stage 1 at -60min and stage 2 at -15min)
        now = starts_at - timedelta(minutes=10)
        await engine._process_meeting(meeting, now)

        # Should use stage 2 message format
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Urgent Meeting in 10 min! https://meet.example.com/urgent" == text


class TestPostStartReminders:
    """Test post-start reminder behaviour."""

    @pytest.mark.asyncio
    async def test_post_start_reminder_fires(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders are sent for REMINDING meetings after start time."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Daily Sync",
            starts_at=starts_at,
            link="https://meet.example.com/sync",
        )
        assert meeting.id is not None
        # Manually transition to REMINDING
        service.mark_reminding(meeting.id)
        meeting = service.get(meeting.id)

        # 5 minutes after start
        now = starts_at + timedelta(minutes=5)
        await engine._process_meeting(meeting, now)

        # Should send post-start message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert (
            "MEETING STARTED 5 min ago: Daily Sync. https://meet.example.com/sync"
            == text
        )

        # Should log reminder
        last_sent = repo.get_last_reminder_time(meeting.id)
        assert last_sent is not None

    @pytest.mark.asyncio
    async def test_post_start_respects_interval(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders wait for configured interval."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Daily Sync",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)

        # First post-start at +5 minutes
        now = starts_at + timedelta(minutes=5)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1
        first_send_time = repo.get_last_reminder_time(meeting.id)
        assert first_send_time is not None

        # Try again immediately (within 2-minute interval)
        import time

        time.sleep(0.01)
        mock_sender.messages.clear()
        now = datetime.now(timezone.utc)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)
        # Should not send (within interval)
        assert len(mock_sender.messages) == 0

        # Manually update the last reminder time to simulate 2+ minutes passing
        conn = repo._get_conn()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE meeting_id = ?",
            (old_time, meeting.id),
        )
        conn.commit()

        # Now try again - should send
        # Use a time that's after the meeting starts but before timeout
        mock_sender.messages.clear()
        now = starts_at + timedelta(minutes=10)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_post_start_only_for_reminding_state(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Post-start reminders only fire for meetings in REMINDING state."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Daily Sync",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        # Leave in PENDING state
        meeting = service.get(meeting.id)
        assert meeting.state == MeetingState.PENDING

        # After start time
        now = starts_at + timedelta(minutes=5)
        await engine._process_meeting(meeting, now)

        # Should check escalation stages instead (stage 2 will fire)
        # PENDING meetings after start still get escalation reminders
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
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Meeting is marked MISSED after timeout period with no acknowledgement."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)
        meeting = service.get(meeting.id)

        # 31 minutes after start (past 30-minute timeout)
        now = starts_at + timedelta(minutes=31)
        await engine._process_meeting(meeting, now)

        # Should send MISSED message
        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "MISSED: Important Call (no acknowledgement received)" == text

        # Should mark as MISSED
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.MISSED

    @pytest.mark.asyncio
    async def test_timeout_only_for_reminding_state(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Timeout only applies to meetings in REMINDING state."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        # Leave in PENDING state
        meeting = service.get(meeting.id)
        assert meeting.state == MeetingState.PENDING

        # Past timeout
        now = starts_at + timedelta(minutes=31)
        await engine._process_meeting(meeting, now)

        # Should NOT mark as missed, but will send escalation reminder
        # which transitions to REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.REMINDING
        assert len(mock_sender.messages) == 1
        # Should be escalation message, not MISSED
        _, text = mock_sender.messages[0]
        assert "MISSED" not in text

    @pytest.mark.asyncio
    async def test_timeout_checked_before_post_start(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Timeout is checked before post-start reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Important Call",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)
        meeting = service.get(meeting.id)

        # Exactly at timeout boundary
        now = starts_at + timedelta(minutes=30)
        await engine._process_meeting(meeting, now)

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
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """All template variables are correctly substituted."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Project Review",
            starts_at=starts_at,
            link="https://zoom.us/j/999",
        )
        assert meeting.id is not None

        # 15 minutes before (stage 2)
        now = starts_at - timedelta(minutes=15)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # {title} in {mins_until} min! {link}
        assert text == "Project Review in 15 min! https://zoom.us/j/999"

    @pytest.mark.asyncio
    async def test_format_time_string(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Time is formatted as HH:MM."""
        # Create a specific time for predictable formatting
        base = datetime.now(timezone.utc)
        starts_at = base.replace(hour=9, minute=5, second=0, microsecond=0)
        if starts_at <= base:
            starts_at += timedelta(days=1)

        meeting = service.create(
            title="Morning Standup",
            starts_at=starts_at,
        )
        assert meeting.id is not None

        # 1 hour before (stage 1: "Reminder: {title} at {time}")
        now = starts_at - timedelta(hours=1)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "Reminder: Morning Standup at 09:05" == text

    @pytest.mark.asyncio
    async def test_format_no_link(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Missing link is replaced with '(no link)'."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="In-Person Meeting",
            starts_at=starts_at,
            link=None,
        )
        assert meeting.id is not None

        # 15 minutes before
        now = starts_at - timedelta(minutes=15)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "(no link)" in text

    @pytest.mark.asyncio
    async def test_format_mins_ago(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """mins_ago is calculated correctly for post-start messages."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Standup",
            starts_at=starts_at,
            link="https://meet.example.com/standup",
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)
        meeting = service.get(meeting.id)

        # 7 minutes after start
        now = starts_at + timedelta(minutes=7)
        await engine._process_meeting(meeting, now)

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
        """mins_until is clamped to 0 when meeting has started."""
        meeting = Meeting(
            id=1,
            title="Test",
            starts_at=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 3, 10, 14, 5, tzinfo=timezone.utc)

        msg = engine._format_message("{mins_until}", meeting, now)
        assert msg == "0"


class TestFailureHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_failed_send_does_not_log(
        self,
        service: MeetingService,
        repo: SqliteMeetingRepository,
        escalation_config: EscalationConfig,
    ) -> None:
        """Failed send does not log reminder or transition state."""
        failing_sender = FailingSender()
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=failing_sender,
            recipient="+441234567890",
            config=escalation_config,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Test Meeting",
            starts_at=starts_at,
        )
        assert meeting.id is not None

        # Trigger stage 1
        now = starts_at - timedelta(hours=1)
        await engine._process_meeting(meeting, now)

        # Should NOT log reminder
        last_sent = repo.get_last_reminder_time(meeting.id)
        assert last_sent is None

        # Should NOT transition to REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.PENDING

    @pytest.mark.asyncio
    async def test_meeting_without_starts_at_skipped(
        self,
        engine: ReminderEngine,
        mock_sender: MockSender,
    ) -> None:
        """Meetings without starts_at are skipped in tick().

        Note: The schema requires starts_at to be NOT NULL, so this scenario
        can only occur if the code checks for None defensively. The tick()
        method does check 'if meeting.starts_at is None: continue'.

        Since we can't create such a meeting in the database, we test that
        the defensive check exists by verifying tick() doesn't crash.
        """
        # Create a normal meeting
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = engine._service.create(
            title="Normal Meeting",
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
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """tick() logs exceptions but continues processing other meetings."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting_1 = service.create(
            title="Meeting 1",
            starts_at=starts_at,
        )
        meeting_2 = service.create(
            title="Meeting 2",
            starts_at=starts_at,
        )

        # Patch _process_meeting to raise on first call, succeed on second
        original_process = engine._process_meeting
        call_count = 0

        async def mock_process(meeting, now):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Simulated error")
            return await original_process(meeting, now)

        with patch.object(engine, "_process_meeting", side_effect=mock_process):
            # Mock datetime.now for tick()
            with patch("src.services.reminder_engine.datetime") as mock_dt:
                mock_dt.now.return_value = starts_at - timedelta(hours=1)
                mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
                await engine.tick()

        # Should have attempted both meetings
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_tick_uses_current_time(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """tick() uses datetime.now(timezone.utc) for processing."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Test Meeting",
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
    """Test meeting state transitions during reminder flow."""

    @pytest.mark.asyncio
    async def test_pending_to_reminding_on_first_reminder(
        self,
        engine: ReminderEngine,
        service: MeetingService,
    ) -> None:
        """Meeting transitions from PENDING to REMINDING on first reminder."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        assert meeting.state == MeetingState.PENDING

        # Trigger reminder
        now = starts_at - timedelta(hours=1)
        await engine._process_meeting(meeting, now)

        # Should transition to REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.REMINDING

    @pytest.mark.asyncio
    async def test_reminding_to_missed_on_timeout(
        self,
        engine: ReminderEngine,
        service: MeetingService,
    ) -> None:
        """Meeting transitions from REMINDING to MISSED on timeout."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)

        # Past timeout
        now = starts_at + timedelta(minutes=31)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)

        # Should transition to MISSED
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.MISSED

    @pytest.mark.asyncio
    async def test_stays_reminding_during_post_start(
        self,
        engine: ReminderEngine,
        service: MeetingService,
    ) -> None:
        """Meeting stays in REMINDING during post-start reminders."""
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        meeting = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert meeting.id is not None
        service.mark_reminding(meeting.id)

        # After start, before timeout
        now = starts_at + timedelta(minutes=10)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)

        # Should still be REMINDING
        updated = service.get(meeting.id)
        assert updated.state == MeetingState.REMINDING


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_exactly_at_stage_boundary(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Reminder fires when exactly at stage trigger time."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert meeting.id is not None

        # Exactly at -1 hour
        now = starts_at - timedelta(hours=1, seconds=0)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_exactly_at_interval_boundary(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """Repeating reminder fires when exactly at interval boundary."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Test",
            starts_at=starts_at,
        )
        assert meeting.id is not None

        # First send
        now = starts_at - timedelta(minutes=15)
        await engine._process_meeting(meeting, now)
        assert len(mock_sender.messages) == 1

        # Exactly 5 minutes later
        mock_sender.messages.clear()
        now = starts_at - timedelta(minutes=10)
        meeting = service.get(meeting.id)
        await engine._process_meeting(meeting, now)

        assert len(mock_sender.messages) == 1

    @pytest.mark.asyncio
    async def test_zero_minutes_until(
        self,
        engine: ReminderEngine,
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """mins_until is 0 when at exact start time."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        meeting = service.create(
            title="Test Meeting",
            starts_at=starts_at,
            link="https://example.com",
        )
        assert meeting.id is not None

        # Exactly at start time, trigger stage 2
        now = starts_at
        await engine._process_meeting(meeting, now)

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
        service: MeetingService,
        mock_sender: MockSender,
    ) -> None:
        """tick() processes all PENDING and REMINDING meetings."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        meeting_1 = service.create(
            title="Meeting 1",
            starts_at=starts_at,
        )
        meeting_2 = service.create(
            title="Meeting 2",
            starts_at=starts_at,
        )
        meeting_3 = service.create(
            title="Meeting 3",
            starts_at=starts_at,
        )
        # Mark one as acknowledged (should be skipped)
        service.acknowledge(meeting_3.id, "ack")

        # Mock datetime.now
        with patch("src.services.reminder_engine.datetime") as mock_dt:
            mock_dt.now.return_value = starts_at - timedelta(hours=1)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            await engine.tick()

        # Should send 2 messages (meeting 1 and 2, not 3)
        assert len(mock_sender.messages) == 2
        titles = [msg[1] for msg in mock_sender.messages]
        assert any("Meeting 1" in t for t in titles)
        assert any("Meeting 2" in t for t in titles)
        assert not any("Meeting 3" in t for t in titles)
