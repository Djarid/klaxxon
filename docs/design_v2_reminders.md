# Klaxxon v2 Design: Reminders Enhancement

**Author:** Jason Huxley (product), Claude (architecture)
**Date:** 2026-03-08
**Status:** Approved, pending implementation
**Method:** ATLAS (Architect, Trace, Link, Assemble, Stress-test)

---

## A - Architect

### App Brief

- **Problem:** Klaxxon is meeting-centric with no recurrence, no general
  reminders, no per-reminder escalation control, and no ability to edit or
  escalate to a second person. It needs to become a general-purpose ADHD
  reminder system.
- **User:** Jason Huxley. ADHD, hypertension (critical medication
  adherence), Green Party political engagement (recurring meetings),
  infrastructure engineer.
- **Success:** Recurring medication reminders that nag until acked (never
  timeout). Editable reminders. Per-reminder escalation profiles with
  opt-in recipient escalation. All existing tests pass with updated names.
  All new code tested.
- **Constraints:**
  - Signal-only notification channel (for now)
  - Existing clean architecture (SOLID, DRY, DI) must be preserved
  - SQLite database (no migration framework, manual schema evolution)
  - No snooze feature (too easy to abuse with ADHD)
  - Escalation to another person is **explicit opt-in per reminder**,
    never automatic
  - Backward compatible: existing one-off reminders with default
    escalation profile must work exactly as before

### Features

1. **Configurable reminder timing** - per-reminder `lead_time_min` and
   `nag_interval_min` with defaults from escalation profile
2. **Edit existing reminders** - PATCH endpoint, web UI only (not Signal)
3. **Repeating reminders** - separate Schedule model that spawns Reminder
   instances (for medication, recurring meetings)
4. **Free text / description field** - `description` field on Reminder and
   Schedule, included in notification templates
5. **Escalation profiles with opt-in recipient escalation** - named
   profiles in config define timing patterns; per-reminder `escalate_to`
   phone number enables overflow notifications to a second person

### Naming Decision

Rename "meetings" to "reminders" throughout the entire codebase:

- API: `/api/reminders` (breaking change, single consumer)
- Model: `Reminder` (was `Meeting`)
- State enum: `ReminderState` (was `MeetingState`)
- Service: `ReminderService` (was `MeetingService`)
- Repository: `ReminderRepository` (was `MeetingRepository`)
- State machine: `ReminderStateMachine` (was `MeetingStateMachine`)
- Signal handler: variable/method renames
- SPA: UI text, variable names, fetch URLs
- DB table: `reminders` (was `meetings`)
- Signal commands: unchanged (`ack`, `skip`, `list`, `help`)

---

## T - Trace

### Data Schema

#### reminders table (was: meetings)

```
reminders
  id              INTEGER PRIMARY KEY AUTOINCREMENT
  title           TEXT NOT NULL
  description     TEXT                          -- NEW: free text notes
  starts_at       TEXT NOT NULL                 -- ISO 8601 UTC
  duration_min    INTEGER DEFAULT 90
  link            TEXT
  source          TEXT DEFAULT 'manual'
  state           TEXT DEFAULT 'pending'
  profile         TEXT DEFAULT 'meeting'        -- NEW: escalation profile name
  escalate_to     TEXT                          -- NEW: E.164 phone number, opt-in
  schedule_id     INTEGER REFERENCES schedules(id) ON DELETE SET NULL  -- NEW
  lead_time_min   INTEGER                       -- NEW: override profile first stage
  nag_interval_min INTEGER                      -- NEW: override profile repeat interval
  ack_keyword     TEXT
  ack_at          TEXT
  created_at      TEXT NOT NULL
  updated_at      TEXT NOT NULL

Indexes:
  idx_reminders_state
  idx_reminders_starts_at
  idx_reminders_schedule_id                     -- NEW
```

#### schedules table (NEW)

```
schedules
  id              INTEGER PRIMARY KEY AUTOINCREMENT
  title           TEXT NOT NULL
  description     TEXT
  time_of_day     TEXT NOT NULL                 -- "HH:MM" local time
  duration_min    INTEGER DEFAULT 0             -- 0 for non-meeting reminders
  link            TEXT
  source          TEXT DEFAULT 'manual'
  profile         TEXT DEFAULT 'meeting'
  escalate_to     TEXT                          -- E.164 phone number, opt-in
  recurrence      TEXT NOT NULL                 -- 'daily', 'weekly', 'custom'
  recurrence_rule TEXT                          -- e.g. "mon,wed,fri"
  lead_time_min   INTEGER
  nag_interval_min INTEGER
  is_active       INTEGER DEFAULT 1
  created_at      TEXT NOT NULL
  updated_at      TEXT NOT NULL

Indexes:
  idx_schedules_active
  idx_schedules_recurrence
```

#### reminder_log table (existing, column rename only)

```
reminder_log
  id              INTEGER PRIMARY KEY AUTOINCREMENT
  reminder_id     INTEGER REFERENCES reminders(id) ON DELETE CASCADE  -- was: meeting_id
  sent_at         TEXT NOT NULL
  message         TEXT NOT NULL
  channel         TEXT DEFAULT 'signal'

Indexes:
  idx_reminder_log_reminder                     -- was: idx_reminder_log_meeting
```

#### auth_tokens table (unchanged)

No changes.

### Relationships

```
schedules 1:N reminders  (via schedule_id)
reminders 1:N reminder_log  (via reminder_id)
```

### Escalation Profile Schema (config.yaml)

```yaml
timezone: "Europe/London"

escalation_profiles:
  meeting:
    stages:
      - offset_hours: -24
        interval_min: null
        target: self
        message: "Tomorrow: {title} at {time}. {link}"
      - offset_hours: -2
        interval_min: null
        target: self
        message: "{title} in 2 hours. {link}"
      - offset_hours: -0.5
        interval_min: 5
        target: self
        message: "{title} in {mins_until} min. {link}"
      - offset_hours: -0.25
        interval_min: 2
        target: self
        message: "{title} in {mins_until} min! {link}"
      - offset_hours: 0
        interval_min: 1
        target: self
        message: "JOIN NOW: {title}. {link}"
    post_start_interval_min: 2
    post_start_target: self
    post_start_message: "MEETING STARTED {mins_ago} min ago: {title}. {link}"
    overflow:
      after_min: 10
      interval_min: 5
      target: escalate
      message: "Jason hasn't joined: {title} (started {mins_ago} min ago)"
    timeout_after_min: 90

  persistent:
    stages:
      - offset_hours: 0
        interval_min: 5
        target: self
        message: "{title}. {description}"
    post_start_interval_min: 5
    post_start_target: self
    post_start_message: "OVERDUE ({mins_ago} min): {title}. {description}"
    overflow:
      after_min: 30
      interval_min: 10
      target: escalate
      message: "Jason hasn't done: {title} ({mins_ago} min overdue)"
    timeout_after_min: null

  gentle:
    stages:
      - offset_hours: -0.25
        interval_min: 15
        target: self
        message: "Reminder: {title}. {description}"
    post_start_interval_min: 15
    post_start_target: self
    post_start_message: "OVERDUE: {title}. {description}"
    overflow:
      after_min: 60
      interval_min: 30
      target: escalate
      message: "Jason hasn't actioned: {title} ({mins_ago} min overdue)"
    timeout_after_min: 480
```

### Escalation Target Resolution

Each escalation stage has a `target` field: `self` or `escalate`.

Resolution logic in `ReminderEngine`:

1. `target: self` always resolves to `SIGNAL_RECIPIENT` (owner's number
   from `.env`)
2. `target: escalate` resolves to the reminder's `escalate_to` field:
   - If `escalate_to` is set (a valid E.164 number): send to that number
   - If `escalate_to` is null/empty: fall back to `self` (owner only)
3. When `target: escalate` resolves to a real number, the engine sends to
   **both** self and escalate (the owner always receives their own
   reminders)

This means:
- No phone numbers in config.yaml (just patterns)
- No contacts database needed (yet)
- The escalation recipient is explicit opt-in per reminder at creation time
- A profile can define overflow to `escalate`, but it only takes effect if
  the individual reminder has `escalate_to` populated

### Validation

- `escalate_to`: regex `^\+[1-9]\d{6,14}$` (E.164 international format)
  or null
- `profile`: must match a key in `escalation_profiles` config; falls back
  to `meeting` with a warning log if not found
- `recurrence`: one of `daily`, `weekly`, `custom`
- `recurrence_rule`: required when recurrence is `weekly` or `custom`;
  comma-separated lowercase day abbreviations (`mon,tue,wed,thu,fri,sat,sun`)
- `time_of_day`: regex `^\d{2}:\d{2}$` (HH:MM, 24-hour)

### Edge Cases

1. **Schedule spawning while app is down:** ScheduleService must handle
   catch-up. Check last spawned occurrence, create any missing ones within
   the spawning window.
2. **Timezone transitions (BST/GMT):** Schedule `time_of_day` is local
   time; resolved against `timezone` config on each spawn using
   `zoneinfo.ZoneInfo`.
3. **Editing a reminder spawned by a schedule:** Edit applies to that
   instance only, not the schedule template.
4. **Deleting a schedule:** `ON DELETE SET NULL` means existing reminders
   become orphans (effectively one-off). No cascade deletion.
5. **Profile name not found in config:** Fall back to `meeting` profile
   with a warning log.
6. **Null timeout with no ack:** `persistent` profile reminders nag
   indefinitely. The `MISSED` state is never reached. This is by design
   for medication adherence (hypertension).
7. **Concurrent ack from Signal and web:** Already handled by state
   machine (second ack gets `InvalidTransitionError`, returns 409).
8. **Overflow without escalate_to:** Overflow stage fires but only sends
   to self. No error, just degrades gracefully.
9. **Per-reminder timing overrides with profile stages:** When
   `lead_time_min` or `nag_interval_min` is set on a reminder, those
   values override the profile's first stage offset and repeating interval
   respectively. The profile's stage structure (number of stages, message
   templates, targets) is still used.

### Integrations Map

| Service | Purpose | Auth | Notes |
|---|---|---|---|
| SQLite | Storage | File path | Manual migration script |
| signal-cli REST API | Notifications + commands | None (local) | One POST per recipient |
| Traefik + Cloudflare | Public access | Bearer token | No changes needed |

### Technology Stack

No changes. Python 3.11, FastAPI, SQLite, Alpine.js SPA, signal-cli REST
API.

---

## Build Order

Dependencies between features dictate the order. Each step is a separate
commit.

### Step 1: Rename meetings to reminders

Mechanical rename across the entire codebase. No logic changes.

**Renames:**

| From | To |
|---|---|
| `Meeting` | `Reminder` |
| `MeetingState` | `ReminderState` |
| `MeetingCreate`, `MeetingResponse`, `MeetingListResponse` | `ReminderCreate`, `ReminderResponse`, `ReminderListResponse` |
| `MeetingRepository` | `ReminderRepository` |
| `SqliteMeetingRepository` | `SqliteReminderRepository` |
| `MeetingService` | `ReminderService` |
| `MeetingStateMachine` | `ReminderStateMachine` |
| `InvalidTransitionError` | (unchanged) |
| `/api/meetings` | `/api/reminders` |
| `meeting_service.py` | `reminder_service.py` |
| `models/meeting.py` | `models/reminder.py` |
| DB table `meetings` | `reminders` |
| `reminder_log.meeting_id` | `reminder_log.reminder_id` |

**File renames:**

- `src/models/meeting.py` → `src/models/reminder.py`
- `src/services/meeting_service.py` → `src/services/reminder_service.py`
- `tests/test_meeting_service.py` → `tests/test_reminder_service.py`

**Files modified (not renamed):**

- `src/models/__init__.py`
- `src/models/schemas.py`
- `src/repository/base.py`
- `src/repository/sqlite.py`
- `src/services/state_machine.py`
- `src/services/reminder_engine.py`
- `src/signal_handler.py`
- `src/api/routes.py`
- `src/config.py`
- `src/main.py`
- `web/index.html`
- `config.yaml`
- All test files

**Verification:** All existing tests pass with updated names.

**Commit:** `Rename meetings to reminders throughout codebase`

---

### Step 2: Add description field

**Changes:**

- Model: add `description: Optional[str] = None` to `Reminder`
- Schema: add `description` to `ReminderCreate` and `ReminderResponse`
- Repository: add column to CREATE TABLE, include in INSERT/SELECT/UPDATE
- ReminderEngine: add `{description}` template variable (empty string if null)
- SPA: add optional "Notes" textarea to create form; show on cards if present
- Migration: `ALTER TABLE reminders ADD COLUMN description TEXT`

**Tests:** ~5 new (create with/without description, API, template rendering)

**Commit:** `Add description field to reminders for free-text notes`

---

### Step 3: Escalation profiles with opt-in recipient escalation

The largest change. Sub-steps:

**3a. Config schema**

New dataclasses in `config.py`:

```python
@dataclass
class EscalationStage:
    offset_hours: float
    interval_min: Optional[int]  # null = single ping
    target: str                  # "self" or "escalate"
    message: str                 # template with {title}, {time}, {link}, etc.

@dataclass
class EscalationOverflow:
    after_min: int               # minutes after trigger with no ack
    interval_min: int
    target: str                  # "self" or "escalate"
    message: str

@dataclass
class EscalationProfile:
    stages: list[EscalationStage]
    post_start_interval_min: int
    post_start_target: str
    post_start_message: str
    overflow: Optional[EscalationOverflow]
    timeout_after_min: Optional[int]  # null = never timeout
```

Config loader parses `escalation_profiles` from YAML into these
dataclasses.

**3b. Reminder model changes**

Add to `Reminder`:
- `profile: str = "meeting"`
- `escalate_to: Optional[str] = None`

Schema validation: `escalate_to` must match E.164 regex or be null.

**3c. ReminderEngine refactor**

1. Look up reminder's `profile` field
2. Load corresponding `EscalationProfile` from config
3. Fall back to `meeting` profile if not found (log warning)
4. Resolve `target` per stage (self → owner, escalate → `escalate_to` or
   fall back to owner)
5. Overflow logic: if N minutes since event trigger with no ack, switch
   to overflow pattern
6. `timeout_after_min: null` → skip timeout transition, keep nagging
7. Send to each resolved recipient individually (one API call per
   recipient via existing `send_message`)

**Migration:**

```sql
ALTER TABLE reminders ADD COLUMN profile TEXT DEFAULT 'meeting';
ALTER TABLE reminders ADD COLUMN escalate_to TEXT;
```

**Tests:** ~25 new (profile loading, stage resolution, overflow, null
timeout, target resolution, escalate_to validation)

**Commit:** `Add escalation profiles with opt-in recipient escalation`

---

### Step 4: Edit existing reminders (PATCH endpoint)

**Changes:**

- API: `PATCH /api/reminders/{id}` - partial update
- Schema: new `ReminderUpdate` model (all fields Optional)
- Service: `ReminderService.update(id, **fields)` with state validation
  (only PENDING or REMINDING)
- Repository: `update_fields(id, fields: dict)` - dynamic UPDATE
- SPA: edit button on cards, pre-populated form, PATCH on submit

**Tests:** ~10 new (happy path, state validation, partial updates, 409 on
terminal states)

**Commit:** `Add PATCH endpoint and SPA edit form for reminders`

---

### Step 5: Recurring reminders (Schedule model)

**New files:**

- `src/models/schedule.py` - Schedule dataclass
- `src/repository/schedule_sqlite.py` - SqliteScheduleRepository
- `src/services/schedule_service.py` - ScheduleService with spawning logic

**Changes:**

- API: CRUD for `/api/schedules` (POST, GET, GET/{id}, PATCH/{id},
  DELETE/{id})
- SPA: new "Schedules" section with create/edit/toggle forms
- Reminder model: add `schedule_id` field
- Main.py: wire ScheduleService, add to scheduler loop

**Spawning logic:**

1. ScheduleService runs alongside ReminderEngine in the scheduler loop
2. For each active schedule, calculate next occurrences within a 48-hour
   window
3. Check if a reminder already exists for each occurrence (`schedule_id` +
   `starts_at`)
4. If not, create one with fields inherited from schedule
5. Handle catch-up for missed spawns (app downtime)

**Recurrence resolution:**

- `daily`: every day at `time_of_day`
- `weekly`: on days in `recurrence_rule` (e.g., `"mon,wed,fri"`) at
  `time_of_day`
- `custom`: same as weekly for now (extensible later)

**Migration:**

```sql
CREATE TABLE schedules (...);
ALTER TABLE reminders ADD COLUMN schedule_id INTEGER
    REFERENCES schedules(id) ON DELETE SET NULL;
CREATE INDEX idx_reminders_schedule_id ON reminders(schedule_id);
```

**Tests:** ~20 new (CRUD, spawning, recurrence, catch-up, deactivation,
schedule-to-reminder field propagation)

**Commit:** `Add Schedule model for recurring reminders`

---

### Step 6: Per-reminder timing overrides

**Changes:**

- Reminder model: add `lead_time_min: Optional[int]` and
  `nag_interval_min: Optional[int]`
- Schedule model: same two fields (propagate to spawned reminders)
- ReminderEngine: check per-reminder overrides before profile defaults
- Schema/API: add both fields as optional to create/update schemas
- SPA: optional "Start reminding (min before)" and "Nag every (min)"
  fields, collapsed under "Advanced" section

**Migration:**

```sql
ALTER TABLE reminders ADD COLUMN lead_time_min INTEGER;
ALTER TABLE reminders ADD COLUMN nag_interval_min INTEGER;
ALTER TABLE schedules ADD COLUMN lead_time_min INTEGER;
ALTER TABLE schedules ADD COLUMN nag_interval_min INTEGER;
```

**Tests:** ~10 new (override vs default, propagation from schedule)

**Commit:** `Add per-reminder timing overrides for lead time and nag interval`

---

## Migration Strategy

Single file: `migrations/001_meetings_to_reminders.sql`

Contains all schema changes for steps 1-6. Run manually once against the
production DB on the API LXC:

```bash
ssh pve1 "sudo pct exec 111 -- sqlite3 /opt/klaxxon/data/klaxxon.db < /opt/klaxxon/migrations/001_meetings_to_reminders.sql"
```

The migration script will be structured with each section clearly
commented. SQLite does not support `IF NOT EXISTS` on `ALTER TABLE`, so
the script assumes a clean run against the current schema.

---

## Test Strategy

| Step | Tests Added | Cumulative Total |
|---|---|---|
| 1. Rename | 0 (all existing updated) | ~139 |
| 2. Description | ~5 | ~144 |
| 3. Profiles | ~25 | ~169 |
| 4. Edit | ~10 | ~179 |
| 5. Schedules | ~20 | ~199 |
| 6. Overrides | ~10 | ~209 |

All tests run with `pytest` from the repo root. No external dependencies
required (SQLite in-memory, mocked Signal client).

---

## State Machine (unchanged states, unchanged transitions)

### States (ReminderState)

- `PENDING` - created, waiting for first reminder
- `REMINDING` - actively sending escalating reminders
- `ACKNOWLEDGED` - user confirmed (ack/joining)
- `SKIPPED` - user deliberately skipped
- `MISSED` - timeout reached with no response (never reached when
  `timeout_after_min` is null)

### Valid Transitions

```
(PENDING,    "reminder_sent") → REMINDING
(REMINDING,  "reminder_sent") → REMINDING   # re-entry
(PENDING,    "ack")           → ACKNOWLEDGED
(REMINDING,  "ack")           → ACKNOWLEDGED
(PENDING,    "skip")          → SKIPPED
(REMINDING,  "skip")          → SKIPPED
(REMINDING,  "timeout")       → MISSED
```

No new states or transitions are needed for this enhancement.

---

## Files Inventory (post-implementation)

```
src/
├── models/
│   ├── reminder.py              # Reminder dataclass + ReminderState enum
│   ├── schedule.py              # Schedule dataclass (NEW)
│   └── schemas.py               # Pydantic request/response schemas
├── repository/
│   ├── base.py                  # ReminderRepository + ScheduleRepository ABCs
│   ├── sqlite.py                # SqliteReminderRepository
│   └── schedule_sqlite.py       # SqliteScheduleRepository (NEW)
├── services/
│   ├── reminder_service.py      # Business logic (was meeting_service.py)
│   ├── schedule_service.py      # Schedule spawning logic (NEW)
│   ├── state_machine.py         # Transition validation
│   ├── reminder_engine.py       # Escalation scheduler
│   └── notification/
│       ├── base.py              # MessageSender/MessageReceiver ABCs
│       └── signal_client.py     # Signal REST API adapter
├── api/
│   ├── routes.py                # HTTP endpoints
│   └── auth.py                  # Bearer token validation
├── signal_handler.py            # Incoming Signal command parser
├── config.py                    # Config loader (YAML + .env)
└── main.py                      # Composition root + scheduler loop

migrations/
└── 001_meetings_to_reminders.sql

tests/
├── conftest.py
├── test_repository.py
├── test_state_machine.py
├── test_reminder_service.py     # was test_meeting_service.py
├── test_reminder_engine.py
├── test_signal_handler.py
├── test_signal_client.py
├── test_api_routes.py
├── test_schedule_service.py     # NEW
└── test_schedule_repository.py  # NEW

docs/
└── design_v2_reminders.md       # This file

web/
├── index.html                   # SPA (updated)
└── style.css                    # Styles (minimal changes)
```

---

## Copyright

This document is part of the Klaxxon project.
CC-BY 4.0 Jason Huxley, 2026.
