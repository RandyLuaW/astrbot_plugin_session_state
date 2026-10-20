# Watchful / active state machine: give group chat a rhythm

> AstrBot plugin · enter active only after joining in, exit on four brakes, modulate by group liveliness

## The problem

The hard part of proactive replies isn't *when to speak* — it's **when to shut up**.

Probability-only plugins hit two extremes: high probability turns into a chatterbox, low probability means silence all night. And once it starts, it tends to stick around — a monologue while the group has moved on.

This plugin turns "speaking up" into a state machine with entry, dwell time and exit:

```
Watchful ──roll / keyword / mentioned──> Active ──four brakes──> Watchful
```

## Four brakes

| Brake | Default | Purpose |
|---|---|---|
| Max active duration | 300 s | Force exit; fixes "won't leave" |
| Cold exit | 90 s of silence | Wrap up when the topic dies |
| Max consecutive turns | 6 | Stop before it becomes a monologue |
| Entry rate limit | 600 s cooldown + 3/hour | Prevents frequent interruptions |

Plus **activity modulation**: speaks up more when the group is quiet, holds back when it's busy.

## Features

- Two-state machine with entry gating, dwell limits and exit reasons
- In active state, follows only **current participants**, ignoring drive-by comments
- Keywords enter directly (bypassing the roll, still rate-limited)
- Won't chime in when the message @s someone else or quotes another user
- Reread filter: identical to the bot's last message (punctuation-stripped) → no response
- `[SKIP]` marker: the model outputs just the marker when the message isn't for it; the plugin swallows it silently
- Quiet hours, including ranges crossing midnight
- `dry_run` mode: full decision logic and logging, but never sends
- Auto-coexists with `active_reply_ctl` (degrades to observe-only when detected)

## Install

### Option 1: Plugin marketplace (recommended)

Search "会话状态机" in the AstrBot WebUI plugin marketplace.

### Option 2: Download from Releases

Grab the zip from [Releases](../../releases), then WebUI → Plugins → Upload.

### Option 3: Clone manually

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_session_state.git
```

No extra dependencies.

## Core configuration

### Switches

| Key | Type | Default | Description |
|---|---|---|---|
| `enable` | bool | `true` | Master switch |
| `dry_run` | bool | `false` | Log decisions without sending — use this first |
| `coexist_mode` | string | `auto` | Relationship with `active_reply_ctl`: `auto` / `on` / `off` |

### Four brakes

| Key | Type | Default | Description |
|---|---|---|---|
| `active_max_duration` | float | `300` | Max seconds in active state |
| `cold_exit_seconds` | float | `90` | Silence threshold for cold exit; 0 disables |
| `max_bot_turns` | int | `6` | Max consecutive bot turns; 0 = unlimited |
| `enter_cooldown` | float | `600` | Min seconds between entries (mentions exempt) |
| `max_enters_per_hour` | int | `3` | Max voluntary entries per rolling hour |

### Entry conditions

| Key | Type | Default | Description |
|---|---|---|---|
| `enter_probability` | float | `0.02` | Base probability per message (~1 in 50) |
| `activity_modulation` | bool | `true` | Modulate by group liveliness |
| `activity_gain` | float | `0.5` | Modulation strength |
| `activity_window` | int | `300` | Liveliness window in seconds |
| `enter_on_keyword` | bool | `true` | Keywords enter directly |
| `keyword_list` | list | `[]` | Trigger keywords; set your own (no defaults shipped) |
| `skip_when_talking_to_others` | bool | `true` | Don't interrupt @s / quotes |
| `min_message_len` | int | `2` | Skip too-short messages (images exempt) |

### Dwell and follow-up

| Key | Type | Default | Description |
|---|---|---|---|
| `follow_participants` | bool | `true` | Only follow current participants |
| `follow_probability` | float | `0.85` | Follow-up response probability |
| `follow_window` | int | `180` | Seconds a participant counts as "still talking" |

### Other

| Key | Type | Default | Description |
|---|---|---|---|
| `quiet_hours` | list | `[]` | e.g. `23:00-08:00`, multiple ranges supported |
| `reread_filter` | bool | `true` | Reread filter |
| `skip_marker` | string | `[SKIP]` | Marker meaning "not for me" |
| `prompt_template` | text | see plugin | Entry prompt; placeholders `{sender}` `{message}` `{marker}` |
| `follow_prompt_template` | text | see plugin | Follow-up prompt |
| `debug` | bool | `false` | Log decision details |

## Tuning

1. **Run `dry_run: true` for two or three days first** and check the logged decisions
2. **Too chatty**: lower `enter_probability`, lower `max_enters_per_hour`
3. **Too quiet**: raise `enter_probability`, raise `activity_gain`
4. **Talks too many turns**: lower `max_bot_turns` (4 is usually enough)
5. **Exits too early**: raise `cold_exit_seconds` to 120–180

## Notes

- **Conflicts with other proactive mechanisms**: overlaps with `active_reply_ctl` and iris_memory's proactive features. `coexist_mode: auto` only detects `active_reply_ctl`; disable the others yourself.
- State is in memory; restart returns to watchful.
- `[SKIP]` relies on the model complying — don't soften the prompt too much.
- Depends on `aiocqhttp` event structures.

## License

MIT
