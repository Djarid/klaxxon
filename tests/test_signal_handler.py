"""Tests for Signal incoming message handler.

Comprehensive test coverage for SignalHandler command parsing and delegation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models.meeting import Meeting, MeetingState
from src.services.notification.base import IncomingMessage
from src.signal_handler import SignalHandler

OWNER_NUMBER = "+441234567890"
OTHER_NUMBER = "+449876543210"

ACK_KEYWORDS = ["ack", "joining"]
SKIP_KEYWORDS = ["skip"]
LIST_KEYWORDS = ["list", "meetings"]
HELP_KEYWORDS = ["help"]


@pytest.fixture
def signal_handler(service, mock_sender, mock_receiver):
    """Signal handler with mock sender/receiver and service."""
    return SignalHandler(
        service=service,
        receiver=mock_receiver,
        sender=mock_sender,
        owner_number=OWNER_NUMBER,
        ack_keywords=ACK_KEYWORDS,
        skip_keywords=SKIP_KEYWORDS,
        list_keywords=LIST_KEYWORDS,
        help_keywords=HELP_KEYWORDS,
    )


# --- Ack Command Tests ---


@pytest.mark.asyncio
async def test_ack_keyword_acknowledges_reminding_meeting(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that 'ack' acknowledges a reminding meeting."""
    # Create a meeting in the future and mark it as reminding
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Team Standup",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)

    # Queue an ack message from owner
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    # Check meeting was acknowledged
    updated = service.get(meeting.id)
    assert updated.state == MeetingState.ACKNOWLEDGED
    assert updated.ack_keyword == "ack"

    # Check confirmation message sent
    assert len(mock_sender.messages) == 1
    recipient, text = mock_sender.messages[0]
    assert recipient == OWNER_NUMBER
    assert "Acknowledged: Team Standup" in text
    assert "Reminders stopped" in text


@pytest.mark.asyncio
async def test_joining_keyword_acknowledges_reminding_meeting(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that 'joining' also acknowledges a reminding meeting."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Project Review",
        starts_at=starts_at,
        duration_min=60,
    )
    service.mark_reminding(meeting.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="joining"))

    await signal_handler.poll()

    updated = service.get(meeting.id)
    assert updated.state == MeetingState.ACKNOWLEDGED
    assert updated.ack_keyword == "joining"

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Acknowledged: Project Review" in text


@pytest.mark.asyncio
async def test_ack_with_no_active_meeting_sends_error(
    signal_handler, mock_sender, mock_receiver
):
    """Test that ack with no active meeting sends error message."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No active meeting to acknowledge."


@pytest.mark.asyncio
async def test_ack_with_already_acknowledged_meeting_sends_no_active_error(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that ack when only acknowledged meetings exist sends no active error."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Already Done",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)
    service.acknowledge(meeting.id, "ack")

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    # Should send "no active meeting" because acknowledged meetings are filtered out
    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No active meeting to acknowledge."


# --- Skip Command Tests ---


@pytest.mark.asyncio
async def test_skip_keyword_skips_reminding_meeting(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that 'skip' skips a reminding meeting."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Optional Meeting",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="skip"))

    await signal_handler.poll()

    updated = service.get(meeting.id)
    assert updated.state == MeetingState.SKIPPED

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Skipped: Optional Meeting" in text
    assert "Reminders stopped" in text


@pytest.mark.asyncio
async def test_skip_with_no_active_meeting_sends_error(
    signal_handler, mock_sender, mock_receiver
):
    """Test that skip with no active meeting sends error message."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="skip"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No active meeting to skip."


@pytest.mark.asyncio
async def test_skip_with_already_skipped_meeting_sends_no_active_error(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that skip when only skipped meetings exist sends no active error."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Already Skipped",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)
    service.skip(meeting.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="skip"))

    await signal_handler.poll()

    # Should send "no active meeting" because skipped meetings are filtered out
    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No active meeting to skip."


# --- List Command Tests ---


@pytest.mark.asyncio
async def test_list_keyword_shows_active_meetings(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that 'list' shows pending and reminding meetings."""
    starts_at1 = datetime.now(timezone.utc) + timedelta(hours=1)
    starts_at2 = datetime.now(timezone.utc) + timedelta(hours=3)

    meeting1 = service.create(
        title="First Meeting",
        starts_at=starts_at1,
        duration_min=30,
    )
    meeting2 = service.create(
        title="Second Meeting",
        starts_at=starts_at2,
        duration_min=60,
    )
    service.mark_reminding(meeting1.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="list"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Upcoming meetings:" in text
    assert "First Meeting" in text
    assert "Second Meeting" in text
    assert "reminding" in text
    assert "pending" in text


@pytest.mark.asyncio
async def test_meetings_keyword_shows_active_meetings(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that 'meetings' also shows active meetings."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    service.create(
        title="Test Meeting",
        starts_at=starts_at,
        duration_min=30,
    )

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="meetings"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Upcoming meetings:" in text
    assert "Test Meeting" in text


@pytest.mark.asyncio
async def test_list_with_no_meetings_sends_empty_message(
    signal_handler, mock_sender, mock_receiver
):
    """Test that list with no meetings sends 'no upcoming' message."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="list"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No upcoming meetings."


@pytest.mark.asyncio
async def test_list_excludes_acknowledged_and_skipped_meetings(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that list only shows pending/reminding, not acked/skipped."""
    starts_at1 = datetime.now(timezone.utc) + timedelta(hours=1)
    starts_at2 = datetime.now(timezone.utc) + timedelta(hours=2)
    starts_at3 = datetime.now(timezone.utc) + timedelta(hours=3)

    meeting1 = service.create(
        title="Pending Meeting",
        starts_at=starts_at1,
        duration_min=30,
    )
    meeting2 = service.create(
        title="Acknowledged Meeting",
        starts_at=starts_at2,
        duration_min=30,
    )
    meeting3 = service.create(
        title="Skipped Meeting",
        starts_at=starts_at3,
        duration_min=30,
    )

    service.mark_reminding(meeting2.id)
    service.acknowledge(meeting2.id, "ack")
    service.mark_reminding(meeting3.id)
    service.skip(meeting3.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="list"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Pending Meeting" in text
    assert "Acknowledged Meeting" not in text
    assert "Skipped Meeting" not in text


# --- Help Command Tests ---


@pytest.mark.asyncio
async def test_help_keyword_sends_help_text(signal_handler, mock_sender, mock_receiver):
    """Test that 'help' sends help text."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="help"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Klaxxon commands:" in text
    assert "ack / joining" in text
    assert "skip" in text
    assert "list / meetings" in text
    assert "help" in text


# --- Non-Owner Message Tests ---


@pytest.mark.asyncio
async def test_ignores_messages_from_non_owner(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that messages from non-owner numbers are ignored."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Private Meeting",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)

    # Message from different number
    mock_receiver.queued.append(IncomingMessage(sender=OTHER_NUMBER, body="ack"))

    await signal_handler.poll()

    # No messages sent, meeting not acknowledged
    assert len(mock_sender.messages) == 0
    updated = service.get(meeting.id)
    assert updated.state == MeetingState.REMINDING


# --- Empty Message Tests ---


@pytest.mark.asyncio
async def test_ignores_empty_message_body(signal_handler, mock_sender, mock_receiver):
    """Test that empty message bodies are ignored."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body=""))
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="   "))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 0


# --- Unknown Command Tests ---


@pytest.mark.asyncio
async def test_ignores_unknown_command(signal_handler, mock_sender, mock_receiver):
    """Test that unrecognised commands are ignored (no reply)."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="unknown"))
    mock_receiver.queued.append(
        IncomingMessage(sender=OWNER_NUMBER, body="random text")
    )

    await signal_handler.poll()

    # No messages sent for unknown commands
    assert len(mock_sender.messages) == 0


# --- Case Insensitivity Tests ---


@pytest.mark.asyncio
async def test_ack_is_case_insensitive(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that ACK, Ack, ack all work."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Case Test",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ACK"))

    await signal_handler.poll()

    updated = service.get(meeting.id)
    assert updated.state == MeetingState.ACKNOWLEDGED
    assert updated.ack_keyword == "ACK"  # Raw keyword preserved


@pytest.mark.asyncio
async def test_skip_is_case_insensitive(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that SKIP, Skip, skip all work."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Case Test",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="SKIP"))

    await signal_handler.poll()

    updated = service.get(meeting.id)
    assert updated.state == MeetingState.SKIPPED


@pytest.mark.asyncio
async def test_list_is_case_insensitive(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that LIST, List, list all work."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    service.create(
        title="Case Test",
        starts_at=starts_at,
        duration_min=30,
    )

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="LIST"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Upcoming meetings:" in text


@pytest.mark.asyncio
async def test_help_is_case_insensitive(signal_handler, mock_sender, mock_receiver):
    """Test that HELP, Help, help all work."""
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="HELP"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Klaxxon commands:" in text


# --- _find_active_meeting Priority Tests ---


@pytest.mark.asyncio
async def test_find_active_meeting_prioritises_reminding_over_pending(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that reminding meetings are returned before pending ones."""
    starts_at1 = datetime.now(timezone.utc) + timedelta(hours=1)
    starts_at2 = datetime.now(timezone.utc) + timedelta(hours=2)

    # Create pending meeting first (earlier start time)
    pending_meeting = service.create(
        title="Pending Meeting",
        starts_at=starts_at1,
        duration_min=30,
    )

    # Create reminding meeting second (later start time)
    reminding_meeting = service.create(
        title="Reminding Meeting",
        starts_at=starts_at2,
        duration_min=30,
    )
    service.mark_reminding(reminding_meeting.id)

    # Ack should target the reminding meeting, not the earlier pending one
    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    # Check that reminding meeting was acknowledged
    updated_reminding = service.get(reminding_meeting.id)
    assert updated_reminding.state == MeetingState.ACKNOWLEDGED

    # Check that pending meeting was not touched
    updated_pending = service.get(pending_meeting.id)
    assert updated_pending.state == MeetingState.PENDING

    # Check confirmation message mentions reminding meeting
    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Reminding Meeting" in text


@pytest.mark.asyncio
async def test_find_active_meeting_returns_pending_when_no_reminding(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that pending meeting is returned when no reminding exists."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    pending_meeting = service.create(
        title="Only Pending",
        starts_at=starts_at,
        duration_min=30,
    )

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    # Pending meeting should be acknowledged
    updated = service.get(pending_meeting.id)
    assert updated.state == MeetingState.ACKNOWLEDGED

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert "Only Pending" in text


@pytest.mark.asyncio
async def test_find_active_meeting_returns_none_when_all_terminal(
    signal_handler, service, mock_sender, mock_receiver
):
    """Test that None is returned when all meetings are in terminal states."""
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    meeting = service.create(
        title="Already Done",
        starts_at=starts_at,
        duration_min=30,
    )
    service.mark_reminding(meeting.id)
    service.acknowledge(meeting.id, "ack")

    mock_receiver.queued.append(IncomingMessage(sender=OWNER_NUMBER, body="ack"))

    await signal_handler.poll()

    assert len(mock_sender.messages) == 1
    _, text = mock_sender.messages[0]
    assert text == "No active meeting to acknowledge."
