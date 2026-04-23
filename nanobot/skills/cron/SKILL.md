---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks.

## Modes

1. **Message mode (`agent_turn`)**
- Use `message` for reminder/agent task prompts.

2. **Command mode (`exec`, preferred for deterministic scripts)**
- Use `command` for direct shell execution.
- Optional `timeout_seconds` controls command timeout.
- Command/RUN jobs do not deliver the shell execution report by default.
- Use `deliver=true` only when you explicitly want the raw cron result sent back.
- Backward compatible with legacy `message="RUN:..."`.

## Examples

Fixed reminder (message mode):
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Recurring script (command mode, preferred):
```
cron(action="add", command="bash /abs/path/run_daily.sh", every_seconds=600, timeout_seconds=180)
```

Cron expression:
```
cron(action="add", command="python /abs/path/job.py", cron_expr="0 9 * * *", timeout_seconds=300)
```

One-shot task:
```
cron(action="add", command="bash /abs/path/one_off.sh", at_time="2026-04-15T09:00:00", timeout_seconds=300)
```

List/remove/enable/run:
```
cron(action="list")
cron(action="remove", job_id="abc123")
cron(action="enable", job_id="abc123", enabled=false)
cron(action="enable", job_id="abc123", enabled=true)
cron(action="run", job_id="abc123", force=true)
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| at 2026-04-15 09:00 | at_time: "2026-04-15T09:00:00" |

## Recommended Practice

- Prefer `command` for production cron jobs requiring deterministic behavior.
- Use `message` when you want the agent to interpret and complete a higher-level task.
- For scripts that send their own notifications, leave `deliver` unset or false.
