---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **Autonomous worker task** - `kind="agent_turn_with_context"` runs in an independent worker session; steer it from the main session with `cron_message`
4. **One-time** - runs once at a specific time, then auto-deletes

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
cron(action="add", message="Check HKUDS/nanobot GitHub stars and report", every_seconds=600)
```

Autonomous worker task:
```
cron(
  action="add",
  kind="agent_turn_with_context",
  message="Every 30 minutes, check the research run status, decide the next step, and report progress.",
  every_seconds=1800
)
```

One-time scheduled task (compute ISO datetime from current time):
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

Timezone-aware cron:
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

List/remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| at a specific time | at: ISO datetime string (compute from current time) |

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.

## Autonomous Worker Tasks

Use `kind="agent_turn_with_context"` for hands-off autonomous loops that should run in an independent cron worker session while sending concise updates back to the current chat session.

- The worker session defaults to `cron:<job_id>`.
- `context_session_key` defaults to the current session and marks who owns/steers the worker.
- `sync_session_key` defaults to `context_session_key` and receives tagged worker summaries.
- Each delivered cron update is tagged as `[From Cron Agent Session: sessions/<worker>.jsonl]`.
- Workers should not call `session_sync`. Main-session agents inject explicit guidance with `cron_message`, which writes a short user message into the worker session and appends a JSONL entry under the research inbox.
- For research loops, keep the recurring cron `message` short and point the worker to a durable state directory instead of repeating a long objective every wake-up.
