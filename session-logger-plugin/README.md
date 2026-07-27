# session-logger — Claude Code plugin

Logs Claude Code session activity to JSONL files in `~/.claude/logs/`.

Each session gets a timestamped log file with events:
- `session_start` / `session_end` (with stats)
- `prompt` — user message text
- `tool_pre` / `tool_post` — every tool call and its outcome

## Install via plugin system

Add the marketplace (once):

```sh
claude plugin marketplace add nazuraki/claude
```

Install the plugin:

```sh
claude plugin install session-logger@nazuraki-claude-plugins
```

## Manual install (symlink, for development)

```sh
git clone <this-repo> ~/src/claude
ln -s ~/src/claude/session-logger-plugin/scripts/session-logger.py ~/.claude/hooks/session-logger.py
```

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/session-logger.py prompt" }] }],
    "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/session-logger.py pre"    }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/session-logger.py tool"   }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/session-logger.py stop"   }] }]
  }
}
```

## Log location

`~/.claude/logs/<timestamp>_<session-short-id>.log`

## Platform support

Each hook event runs as its own process, so the session map and the log files
are written under an exclusive file lock. The backend is chosen at import time:

| Platform | Backend | Notes |
|---|---|---|
| macOS / Linux | `fcntl.flock()` | Whole-file advisory lock |
| Windows | `msvcrt.locking()` | One-byte range lock at a fixed offset |
| Anything else | no-op | Logger still runs; registrations can be lost |

Without a lock the unlocked read-modify-write in `_update_session_map()` loses
all but one registration whenever sessions start simultaneously — measured on
Windows, 8 concurrent starts kept 1. A session missing from the map
re-registers on its next hook event, so its log fragments across files.

Two Windows-specific details are worth knowing before touching this code:

- **The lock is a byte range, not the whole file.** It is always taken on a
  single byte at a fixed 1 TiB offset. Locking at the current position would
  be useless, because `open(log, "a")` starts at EOF and two appenders whose
  files differ in length would lock disjoint ranges.
- **Windows range locks are mandatory, not advisory.** Unlocked reads
  overlapping a locked range fail with `PermissionError`, and reading a whole
  file requests one byte past EOF — so a lock at or below EOF would break
  `handle_stop()`'s unlocked `read_text()`. The 1 TiB offset keeps the lock
  clear of anything a reader, appender or `stat()` touches, and `O_APPEND`
  ignores the seek so no sparse file is created.

## Tests

```sh
python -m unittest discover session-logger-plugin/tests
```

Standard library only — no dependencies. The concurrency tests spawn real
subprocesses, since the races they cover only exist between processes.
