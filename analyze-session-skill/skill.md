---
name: analyze-session
description: "Analyze a Claude Code chat session log for inefficient or incorrect tool usage and recommend skill/plugin/config updates. Examples: \"Analyze this session\", \"What was inefficient in my last chat?\", \"Review tool usage patterns\""
---

# Session Tool-Use Analyzer

Analyze a Claude Code session log to identify inefficient, redundant, or misrouted tool usage and recommend actionable config/skill/plugin improvements.

## Invocation

The user may provide:
- A session UUID or log file path
- "last session" or "current session" (resolve from `~/.claude/projects/`)
- No argument → default to the **current** session when it contains substantive tool use (the usual case: the user invokes this right after the work they want reviewed). Fall back to the most recent *completed* session only if the current one is empty or has no real tool activity yet.

## Step 1 — Locate the log

Session logs live at:
```
~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl
```

**When the target session ID is known — go straight to the path; don't enumerate.** Both the UUID *and* the `<encoded-project-path>` are sitting in the scratchpad path in your environment: `…\Temp\claude\<encoded-project-path>\<session-uuid>\scratchpad`. The `<encoded-project-path>` is the working directory with path separators replaced by `-` — and **in a git-worktree session it is the _worktree_ path, not the base repo** (e.g. `R--repos-Tooling--claude-worktrees-<name>`, NOT `R--repos-Tooling`). Derive the stem from the scratchpad path rather than guessing the base repo; then construct `~/.claude/projects/<encoded-project-path>/<uuid>.jsonl` and it's a straight Read — no search at all.

If that constructed path misses (or you were handed only a bare UUID), fall back to a **UUID-stem-scoped Glob**: `Glob pattern="**/<uuid>*.jsonl" path="~/.claude/projects"`. A UUID is unique, so this returns ~1 file — it does NOT flood. Never reach for shell `find -name` here: it yields no clickable results and trips the search-route hook (`find` filename-search is exactly a Glob job). And do NOT Glob the whole project dir with `*.jsonl` — that can return hundreds of logs (a large, useless result).

Only when you must *discover* which session to use (e.g. "last session" with no ID) enumerate by modification time, and even then narrow to the most recent handful. For "last session", exclude the current session ID if it's still active; for the no-argument default, the current session is the intended target (see Invocation).

Also check for subagent logs in `<session-uuid>/subagents/*.jsonl` — scope that Glob to the session's own stem, not the whole tree.

## Step 2 — Parse and extract tool events

**Do NOT `Read` the raw `.jsonl` into context.** Session logs are dominated by deferred-tool and skill-listing attachment blobs on the first several lines — often >100K tokens even for a short 5-turn session — so a raw `Read` burns context on near-zero signal and truncates before it reaches the actual tool events. Parse programmatically instead.

Run the bundled parser, which strips the blob noise and prints the tool timeline, system/hook events, summed `message.usage` token totals, and per-turn context-window occupancy:

```
python "<skill-dir>/parse_session.py" "<path-to-session>.jsonl"
```

`<skill-dir>` is this skill's own directory (on this machine: `C:\Users\roshn\.claude\skills\analyze-session`). Extend the script if a session needs a field it doesn't yet extract. Only `Read` narrow, known line ranges (`offset`/`limit`) if you must inspect one specific event verbatim.

The parser extracts, per line:
- **assistant messages** (`type: "assistant"`): `message.content[]` blocks where `type == "tool_use"` — `name`, an `input` summary, and the `message.usage` token counts (plus `thinking`/`text` block sizes) — plus `ctx`, that turn's context-window occupancy
- **user messages** (`type: "user"`): when `toolUseResult` is present, the tool result status and content size
- **system messages** (`type: "system"`): hook summaries, errors, denied tools
- **context window**: a trailing `=== CONTEXT WINDOW ===` section — peak/final occupancy with the model at peak, `COMPACTION` rows (observed *and* harness-reported pre/post), `CTX_DROP` rows for occupancy loss with no marker, and per-phase context contribution

From that, build a timeline of: `[turn_number, tool_name, input_summary, output_size, tokens_used, ctx_occupancy, status]`.

Process subagent logs (`<session-uuid>/subagents/*.jsonl`) the same way — run the parser on each — and nest them under their parent tool call.

## Step 3 — Analyze for inefficiencies

Check each pattern below. For every finding, record the turn number(s), what happened, and what should have happened.

### Routing violations
- **Bash/PowerShell for search**: shell running `grep`, `rg`, `find`, `cat`, `head`, `tail`, `ls`, `Select-String`, `Get-Content`, `Get-ChildItem -Recurse` as the *primary* intent → should have used the Grep, Glob, or Read tool. Note: `… | tail -N` / `| head -N` piped onto a legitimate command (git, luacheck, busted, a build) is fine — only flag when search/read *is* the command.
- **Wrong shell for the job**: Unix syntax pushed through the PowerShell tool, or PowerShell/here-string syntax pushed through the Bash tool. Per CLAUDE.md, keep each shell's idioms in its own tool (Git Bash for git/gh/lua/build; PowerShell for Windows-native file/text ops).
- **Grep flood**: Grep with `output_mode: "content"` returning >50 lines → should have narrowed with `glob`/`type`, used `head_limit`, or used `files_with_matches`/`count` mode first to locate before pulling content.
- **Read on large file without offset/limit**: files >500 lines read in full when only a portion was needed → Read with `offset`/`limit`, or Grep to the relevant line first.
- **Raw curl/wget in Bash for web content**: should have been the WebFetch tool (or WebSearch for discovery).
- **Manual API guessing for WoW code**: hand-searching Blizzard API behavior instead of the `mcp__wow-api__*` tools (`lookup_api`, `search_api`, `get_namespace`, `get_widget_methods`, `get_event`, `get_enum`, `list_deprecated`) when working on suite addons.
- **Manual `gh`/git-forge poking where MCP is cleaner**: multi-step issue/PR/repo queries done as raw `gh` loops when a `mcp__…github…__*` tool (list_issues, search_issues, pull_request_read, etc.) is one call. (Simple one-shot `gh` calls are fine.)

### Redundant work
- **Same file read multiple times**: without edits in between (iterative edit-then-reread is fine; flag pure re-reads).
- **Repeated similar Grep/Glob**: patterns that overlap or could be combined into one.
- **Agent spawned for a simple lookup**: an Explore/general-purpose subagent used where a single Grep/Glob would have sufficed.
- **Broad sweep done inline instead of delegated**: many Read/Grep calls fanning across files to answer one question → the Explore agent returns just the conclusion without dumping every file into the main context.
- **Independent tool calls not batched in one message**: sequential calls with no data dependency (e.g. several Reads, or a Grep + a Glob) that should have been issued in a single assistant turn to run in parallel.

### Missed opportunities
- **No skill used where one fits**: manual work that an available skill automates — e.g. commit/merge done by hand instead of via `git-commit-safety`/`pr`, an issue worked without `work-on`, a standards check without `audit`.
- **Impact analysis skipped before edit**: editing a shared function/class/`ns` field without first Grepping for its call sites when the change could ripple across files.
- **No locate-before-deep-dive**: jumping straight to full-file Reads without a Grep/Glob (or Explore agent) to find the right file/lines first.
- **Dead code left untouched**: refactored or deleted code without Grepping for remaining references.
- **Question left to guess instead of asked**: proceeded on an ambiguous, user-owned decision where AskUserQuestion was warranted (or, conversely, asked when a sensible default existed).

### Token waste
- **Large tool outputs**: any single tool result >10KB entering context
- **Thinking tokens disproportionate**: thinking tokens >3x output tokens on simple tasks. *Caveat*: Claude Code logs do not store replayable thinking text, so per-block thinking size is not recoverable — the parser flags such blocks `redacted (size not logged)` and this check is effectively N/A from a log alone. Extended-thinking cost is already folded into `output_tokens`.
- **Cache miss patterns**: low `cache_read_input_tokens` relative to `cache_creation_input_tokens` across turns

### Context pressure
Read these from `=== CONTEXT WINDOW ===`. `ctx` on a TIMELINE row is that turn's occupancy, so the jump between two consecutive `ctx` values attributes the growth to the tool call between them.
- **Runaway occupancy**: `peak_ctx` against the window of `peak_model` — opus-class runs to ~1M, sonnet/haiku-class to ~200K, and the model can change mid-session, so key the denominator off `peak_model`, never a constant. Past ~80% the session is one large tool result away from compacting.
- **What filled the window**: find the largest turn-to-turn `ctx` increase and name the tool call on that row. That is the single most expensive thing the session did to its own context, and it is usually the most actionable finding in the report.
- **Compaction**: a `COMPACTION` row means the session ran out of window and the transcript was rewritten — everything before it was summarized away. `obs_pre`/`obs_post` are what the API saw; `meta_pre`/`meta_post` are the harness's count of the transcript alone and run 2.5–5× lower on the post side. Quote them separately; never average them.
- **Unexplained drops**: a `CTX_DROP` row is occupancy falling with no compaction marker. It is either silent harness-side context clearing (no log entry at all) or a rewound/edited turn re-branching the conversation (file order is not conversation order). Say which you believe and why — do not report it as a compaction.
- **`CONTEXT_CONSUMED`** is cumulative context pushed through the window across all phases; with no compaction it equals `final_ctx`.

### Hook/enforcement failures
- **Denied tool calls**: tools the user rejected — why, and should the permission config change?
- **Hook errors**: any hook failures in system messages

## Step 4 — Generate report

Write the report to the **session-analysis directory**, NEVER into a project/code repo (a session-analysis file must never show up in `git status`):

```
R:\repos\artifacts\Analysis\<project>-session-analysis-<short-uuid>-<YYYY-MM-DD>.md
```

e.g. `wow-session-analysis-b22ec335-2026-07-08.md`. Use the encoded-project-path stem (e.g. `wow`) as the `<project>` prefix to match the existing `wow-*` naming. Do not write to the current working directory.

Files in this directory are pruned automatically: a Windows scheduled task (`ClaudeAnalysisCleanup`, script at `R:\repos\Tooling\PowerShell\Cleanup-AnalysisArtifacts.ps1`) deletes anything older than 30 days each day. Treat these reports as ephemeral — anything worth keeping long-term belongs in a memory file, not here.

Structure:

```markdown
# Session Analysis: <session-id>

**Date**: <timestamp>
**Turns**: <count> | **Tool calls**: <count> | **Total tokens**: <sum> | **Peak context**: <peak_ctx>

## Summary
<2-3 sentence overview of the session's efficiency>

## Findings

### Critical (config changes needed)
<findings that would save significant tokens or prevent errors — each with turn reference, what happened, what should happen, and specific config/skill change>

### Moderate (workflow improvements)
<findings that would improve efficiency>

### Minor (nice-to-have)
<small optimizations>

## Recommended Changes

### CLAUDE.md updates
<specific additions/edits to routing rules>

### Skill updates
<new skills or skill modifications>

### Plugin/hook updates
<hook changes, permission changes>

### Settings changes
<settings.json modifications>

## Token Budget
| Category | Tokens | % of Total |
|----------|--------|------------|
| Cache reads | | |
| Cache creation | | |
| Output | | |
| Input (fresh) | | |
| **Total** | | |

## Context Window
**Peak**: <peak_ctx> (<peak_model>, line <peak_line>) | **Final**: <final_ctx> | **Compactions**: <n>
<what drove the peak — the specific tool call — and any COMPACTION / CTX_DROP events with their line numbers>
```

Pull each row straight from the summed `message.usage` fields: `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, `input_tokens`. A high cache-read share is the cache **working**, not waste — call that out so a big total isn't misread as overspend. Real "fresh" cost = output + input + cache creation.

Fill the Context Window section from `=== CONTEXT WINDOW ===`. Note that `SUM_ALL` is **not** context occupancy — it counts the same cached prefix once per turn and will be an order of magnitude above the window; `peak_ctx` is how full the window actually got. Never present `SUM_ALL` as a context-usage figure.

## Step 5 — Present to user

After writing the file, present:
1. The file path
2. The Summary section inline
3. Count of findings per severity
4. The top 3 most impactful recommended changes

Do NOT present the full report inline — it's in the file.
