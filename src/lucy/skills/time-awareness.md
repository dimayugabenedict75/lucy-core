---
version: 1.0
author: Lucy Core
license: MIT
name: time-awareness
description: Provides real-time date, time, and relative duration.
trigger: ^(what time|current time|date|today|now|time)
---

# Time Awareness
The agent provides the current date, time, and specific temporal context.

## Actions
- **Current Time**: Returns the current hour, minute, and second.
- **Current Date**: Returns the full date (Day, Month, Year).
- **Time Context**: Provides the day of the week and the time of day (e.g., morning, afternoon, evening).
- **Relative Time**: Calculates the difference between two timestamps or points in time.

## Examples
- "What time is it?" -> "It is currently 10:45 AM."
- "What is today's date?" -> "Today is Thursday, October 24, 2024."
- "How long until 5 PM?" -> "There are 6 hours and 15 minutes remaining."
