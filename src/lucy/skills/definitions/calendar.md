---
name: calendar
description: Proactive scheduling and calendar management. Stores events/meetings with datetime, sends advance reminders, and answers 'what's my 2pm meeting?' without explicit prompting.
triggers: calendar|schedule|meeting|appointment|remind|event|what.*(am|is).*[0-9]|when.*meet|busy|agenda
category: productivity
---

## Calendar Skill

**Purpose:** Turn Lucy from reactive to proactive on scheduling. Instead of waiting for you to ask "What's on my calendar?", Lucy will proactively surface upcoming events at the right time.

## Core Capabilities

1. **Event Storage** — SQLite-backed calendar table (`calendar_events`)
   - `id, title, description, start_time, end_time, location, attendees, reminder_minutes, notification_sent`
2. **Natural Language Queries** — "What's my 2 PM meeting?" → parses time + date
3. **Proactive Reminders** — Cron job checks every 5 minutes for upcoming events
4. **TTS Integration** — Speaks reminders as voice bubbles (Cielvox TTS)

## Endpoints
- `POST /api/calendar/event` — Create event
- `GET /api/calendar/events?date=YYYY-MM-DD` — List events for a date
- `GET /api/calendar/events/upcoming` — Next N events
- `DELETE /api/calendar/event/{id}` — Cancel event
- `POST /api/calendar/remind` — Manual reminder check

## Integration Points
- Cron: every 5 min → query upcoming events → TTS notification via Hermes voice channel
- Agent tool: `calendar_query(query: str)` — natural language "What's on my calendar for Thursday?"