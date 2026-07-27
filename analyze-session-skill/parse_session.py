"""Parse a Claude Code session .jsonl into an analyzable timeline.

Session logs are dominated by deferred-tool and skill-listing attachment blobs
on the first several lines (often >100K tokens even for a short session), so
`Read`-ing the raw file wastes context and truncates before the real tool
events. This script strips that noise and prints:

  1. A per-line TIMELINE of tool_use calls (name + compact input summary),
     assistant text/thinking block sizes, and tool_result sizes/status.
  2. SYSTEM/EVENTS — hook summaries, errors, denied tools, parse errors.
  3. USAGE TOTALS — summed message.usage fields, plus the real "fresh" cost
     (output + fresh input + cache creation; cache reads are the cache working).
  4. CONTEXT WINDOW — per-turn context occupancy (peak/final), compaction
     events with both measurements, unexplained occupancy drops, and the
     per-phase context contribution.

Notes on the TIMELINE:
  - Claude Code logs each assistant content block as its own JSONL line but
    repeats the same `message.usage` on every one. Usage totals are therefore
    accumulated once per unique `message.id`, and `out_tokens` is attached to a
    single row per message, so summing `out_tokens` across rows equals the true
    total. Do not expect it on every row.
  - Thinking blocks show only `chars` when readable text is present; Claude Code
    logs usually store no replayable thinking text, so those rows are flagged
    `redacted (size not logged)` rather than a misleading `chars: 0`.

Notes on CONTEXT WINDOW:
  - Occupancy is input + cache_read + cache_creation for one message: what the
    API was asked to hold that turn. output_tokens is excluded — it is
    generated, not sent. It is also attached to the TIMELINE as `ctx`, on the
    same row as `out_tokens`, so growth can be blamed on the tool call that
    caused it.
  - USAGE TOTALS is NOT occupancy: it counts the same cached prefix once per
    turn, so SUM_ALL runs far above the window size and says nothing about how
    full the window got.
  - Compactions are detected on the system/compact_boundary line (which carries
    the harness's own compactMetadata) with the isCompactSummary user line as a
    fallback for older logs. The observed (obs_*) and harness-reported (meta_*)
    numbers measure different prompts and are reported side by side, never
    mixed — the phase math uses obs_* only.
  - Occupancy can also fall with no compaction marker at all (silent
    harness-side context clearing, or a rewound turn re-branching the
    conversation). Those show up as CTX_DROP rows rather than being silently
    charted as a smooth line.
  - Sidechain (subagent) messages are deliberately not filtered out: main
    session logs contain none, and filtering them would zero this whole section
    when the parser is pointed at a subagents/*.jsonl log — which it is meant
    to be.

Usage:
    python parse_session.py <path-to-session>.jsonl

Run it once per log, including each subagent log under
<session-uuid>/subagents/*.jsonl, and nest subagent timelines under the
parent tool_use that spawned them.
"""
import json
import sys

# Windows consoles default to cp1252; report content (arrows, box-drawing, emoji
# in tool inputs) would otherwise crash the json.dumps prints with
# UnicodeEncodeError. Force UTF-8 stdout so the parser never dies on non-ASCII
# session content. (No-op if stdout can't be reconfigured, e.g. already wrapped.)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

if len(sys.argv) < 2:
    sys.exit("usage: python parse_session.py <path-to-session>.jsonl")

path = sys.argv[1]
rows = []
usage_tot = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}
seen_msg_ids = set()
events = []

# Context-window occupancy: how full the window was on each turn, tracked
# separately from USAGE TOTALS (which counts the same cached prefix once per
# turn and so says nothing about how full the window actually got).
ctx_last = 0
ctx_peak = 0
ctx_peak_line = 0
ctx_peak_model = ""
ctx_msgs = 0
ctx_phases = []
ctx_drops = []
awaiting_post = False
# Real logs show 20K-250K occupancy drops with no compaction marker at all —
# silent harness-side context clearing, or a rewound turn re-branching the
# conversation. 20K is well clear of ordinary turn-to-turn jitter.
drop_min = 20000

with open(path, "r", encoding="utf-8") as f:
    for lineno, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception as e:
            events.append((lineno, "PARSE_ERROR", str(e)[:80]))
            continue
        t = o.get("type")
        if t == "assistant":
            msg = o.get("message", {})
            u = msg.get("usage", {}) or {}
            # Each content block is a separate JSONL line repeating the same
            # message.usage, so accumulate usage (and attach out_tokens) only the
            # first time we see a message id. Logs without an id fall back to
            # per-line counting.
            mid = msg.get("id")
            first_of_msg = (mid not in seen_msg_ids) if mid else True
            ctx = 0
            if first_of_msg:
                for k in usage_tot:
                    usage_tot[k] += u.get(k, 0) or 0
                if mid:
                    seen_msg_ids.add(mid)
                # Occupancy is what the API was asked to hold this turn: fresh
                # input plus everything read from or written to the cache.
                # output_tokens is not part of it — it is generated, not sent.
                # Synthetic entries (API errors, spend-limit notices) carry an
                # all-zero usage and would punch false floors into the series.
                if msg.get("model") != "<synthetic>":
                    ctx = ((u.get("input_tokens", 0) or 0)
                           + (u.get("cache_read_input_tokens", 0) or 0)
                           + (u.get("cache_creation_input_tokens", 0) or 0))
                if ctx > 0:
                    ctx_msgs += 1
                    if awaiting_post:
                        # First real turn after a compaction — its occupancy is
                        # the post-compaction floor as the API actually saw it.
                        ctx_phases[-1]["obs_post"] = ctx
                        awaiting_post = False
                    elif ctx_last - ctx >= drop_min:
                        ctx_drops.append({"line": lineno, "kind": "CTX_DROP",
                                          "from": ctx_last, "to": ctx,
                                          "drop": ctx_last - ctx})
                    if ctx > ctx_peak:
                        ctx_peak = ctx
                        ctx_peak_line = lineno
                        ctx_peak_model = msg.get("model") or ""
                    ctx_last = ctx
            content = msg.get("content", []) or []
            msg_rows = []
            texts = []
            for b in content:
                bt = b.get("type")
                if bt == "tool_use":
                    inp = b.get("input", {})
                    keys = {}
                    for kk, vv in inp.items():
                        s = json.dumps(vv) if not isinstance(vv, str) else vv
                        keys[kk] = (s[:90] + "…") if len(s) > 90 else s
                    msg_rows.append({
                        "line": lineno,
                        "kind": "TOOL_USE",
                        "name": b.get("name"),
                        "input": keys,
                    })
                elif bt == "text":
                    txt = b.get("text", "")
                    if txt.strip():
                        texts.append(len(txt))
                elif bt == "thinking":
                    th = b.get("thinking", "") or ""
                    if th.strip():
                        msg_rows.append({"line": lineno, "kind": "THINKING",
                                         "chars": len(th)})
                    else:
                        # Claude Code logs don't store replayable thinking text,
                        # so size is not recoverable — flag it rather than emit 0.
                        msg_rows.append({"line": lineno, "kind": "THINKING",
                                         "note": "redacted (size not logged)"})
            if texts:
                msg_rows.append({"line": lineno, "kind": "ASSISTANT_TEXT",
                                 "text_chars": sum(texts)})
            # Attach message-level output_tokens once per message (on the first
            # block-line seen for it) so summing out_tokens across rows equals
            # the true total (see USAGE TOTALS).
            if msg_rows and first_of_msg:
                msg_rows[-1]["out_tokens"] = u.get("output_tokens", 0)
                if ctx:
                    msg_rows[-1]["ctx"] = ctx
            rows.extend(msg_rows)
        elif t == "user":
            # Every compaction in current logs writes a system/compact_boundary
            # line immediately followed by this isCompactSummary user line, so
            # the boundary — which carries compactMetadata — is the primary
            # detector and this is only a fallback for older logs that lack it.
            # awaiting_post is already set when the normal pair fired.
            if o.get("isCompactSummary") and not awaiting_post:
                ctx_phases.append({"line": lineno, "kind": "COMPACTION",
                                   "trigger": "?", "obs_pre": ctx_last,
                                   "obs_post": 0})
                awaiting_post = True
            tur = o.get("toolUseResult")
            if tur is not None:
                msg = o.get("message", {})
                content = msg.get("content", [])
                size = 0
                status = "ok"
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            size += len(json.dumps(c))
                            if b.get("is_error"):
                                status = "ERROR"
                rows.append({"line": lineno, "kind": "TOOL_RESULT",
                             "result_chars": size, "status": status})
        elif t == "system":
            sub = o.get("subtype") or o.get("level") or ""
            cont = o.get("content") or o.get("message") or ""
            if isinstance(cont, dict):
                cont = json.dumps(cont)
            events.append((lineno, "SYSTEM:" + str(sub), str(cont)[:160]))
            if o.get("subtype") == "compact_boundary":
                # The boundary carries the harness's own measurement. Keep it
                # beside the occupancy we observe from message.usage but never
                # mix the two: meta_post counts only the rewritten transcript,
                # while the next real request also re-sends the system prompt,
                # tool schemas and re-injected attachments, so obs_post runs
                # 2.5-5x higher. The phase math uses the obs_* pair only.
                cm = o.get("compactMetadata") or {}
                phase = {"line": lineno, "kind": "COMPACTION",
                         "trigger": cm.get("trigger") or "?",
                         "obs_pre": ctx_last, "obs_post": 0}
                if cm:
                    phase["meta_pre"] = cm.get("preTokens")
                    phase["meta_post"] = cm.get("postTokens")
                    phase["duration_ms"] = cm.get("durationMs")
                ctx_phases.append(phase)
                awaiting_post = True

print("=== TIMELINE ===")
for r in rows:
    print(json.dumps(r, ensure_ascii=False))
print("\n=== SYSTEM/EVENTS ===")
for e in events:
    print(e)
print("\n=== USAGE TOTALS ===")
print(json.dumps(usage_tot, indent=2))
tot = sum(usage_tot.values())
print("SUM_ALL:", tot)
print("FRESH (out+in+cache_create):",
      usage_tot["output_tokens"] + usage_tot["input_tokens"] + usage_tot["cache_creation_input_tokens"])

# Phase 1 ran from session start to the first compaction, so it contributed its
# whole pre-compaction occupancy; every later phase only added what it grew on
# top of the post-compaction floor it inherited. The trailing phase is skipped
# when the last compaction had no following turn (obs_post stays 0) — there is
# nothing to subtract from and its context is already counted. Contributions are
# clamped at 0 because occupancy is not monotonic within a phase (see CTX_DROP),
# so a raw subtraction can go negative.
phase_contrib = []
if ctx_last > 0:
    if not ctx_phases:
        phase_contrib.append(ctx_last)
    else:
        phase_contrib.append(max(ctx_phases[0]["obs_pre"], 0))
        for i in range(1, len(ctx_phases)):
            phase_contrib.append(max(ctx_phases[i]["obs_pre"] - ctx_phases[i - 1]["obs_post"], 0))
        if ctx_phases[-1]["obs_post"] > 0:
            phase_contrib.append(max(ctx_last - ctx_phases[-1]["obs_post"], 0))

print("\n=== CONTEXT WINDOW ===")
print(json.dumps({
    "messages": ctx_msgs,
    "peak_ctx": ctx_peak,
    "peak_line": ctx_peak_line,
    "peak_model": ctx_peak_model,
    "final_ctx": ctx_last,
    "compactions": len(ctx_phases),
    "unexplained_drops": len(ctx_drops),
}, indent=2))
for p in ctx_phases:
    print(json.dumps(p, ensure_ascii=False))
for d in ctx_drops:
    print(json.dumps(d, ensure_ascii=False))
print("PHASE_CONTRIB:", phase_contrib)
print("CONTEXT_CONSUMED:", sum(phase_contrib))
