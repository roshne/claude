#!/usr/bin/env python3
"""End-to-end tests for the hook CLI contract.

test_locking.py drives the module's internals; these tests drive the script the
way Claude Code actually does -- as a subprocess, one event per invocation,
payload on stdin -- with HOME redirected to a temp directory.

The overriding requirement is that a hook must never crash or hang the session,
so the failure paths (malformed stdin, unknown event, missing fields) matter as
much as the happy path.

Run with:  python -m unittest discover session-logger-plugin/tests
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "session-logger.py"
SID = "e2e12345-aaaa-bbbb-cccc-000000000001"


class HookContractTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        self.env["USERPROFILE"] = self._tmp.name   # Path.home() on Windows
        self.env["HOME"] = self._tmp.name          # Path.home() on POSIX
        self.logs = pathlib.Path(self._tmp.name) / ".claude" / "logs"

    def tearDown(self):
        self._tmp.cleanup()

    def hook(self, event, payload):
        """Invoke the script as Claude Code does; assert it exits cleanly."""
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), event],
            input=raw, text=True, capture_output=True, env=self.env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"{event} exited {proc.returncode}: {proc.stderr.strip()}")
        self.assertEqual(proc.stderr.strip(), "",
                         f"{event} wrote to stderr: {proc.stderr.strip()}")
        return proc

    def records(self):
        files = sorted(self.logs.glob("*.log"))
        self.assertEqual(len(files), 1, f"expected one log file, got {files}")
        return [json.loads(x) for x in files[0].read_text().splitlines() if x.strip()]


class TestFullSession(HookContractTestCase):

    def test_a_whole_session_produces_one_log_with_correct_stats(self):
        self.hook("prompt", {"session_id": SID, "prompt": "add a widget"})
        self.hook("pre", {"session_id": SID, "tool_name": "Read",
                          "tool_input": {"file_path": "/x/y.py"}})
        self.hook("tool", {"session_id": SID, "tool_name": "Read",
                           "tool_input": {"file_path": "/x/y.py"},
                           "tool_response": {"ok": True}})
        self.hook("pre", {"session_id": SID, "tool_name": "Bash",
                          "tool_input": {"command": "ls -la"}})
        self.hook("tool", {"session_id": SID, "tool_name": "Bash",
                           "tool_input": {"command": "ls -la"},
                           "tool_response": {"is_error": True, "error": "boom"}})
        # A pre with no matching post is a denied tool call.
        self.hook("pre", {"session_id": SID,
                          "tool_name": "mcp__github__list_issues", "tool_input": {}})
        self.hook("stop", {"session_id": SID})

        records = self.records()
        self.assertEqual(
            [r["event"] for r in records],
            ["session_start", "prompt", "tool_pre", "tool_post",
             "tool_pre", "tool_post", "tool_pre", "session_end"])
        self.assertEqual(
            records[-1]["stats"],
            {"prompts": 1, "tools_completed": 2, "tools_errored": 1, "tools_denied": 1})
        self.assertTrue(all("ts" in r for r in records), "a record is missing ts")

    def test_events_carry_readable_tool_names_and_details(self):
        self.hook("tool", {"session_id": SID, "tool_name": "Read",
                           "tool_input": {"file_path": "/x/y.py"},
                           "tool_response": {}})
        self.hook("tool", {"session_id": SID, "tool_name": "mcp__github__list_issues",
                           "tool_input": {}, "tool_response": {}})
        posts = [r for r in self.records() if r["event"] == "tool_post"]
        self.assertEqual([p["tool"] for p in posts], ["Read", "github.list_issues"])
        self.assertEqual(posts[0]["detail"], "/x/y.py")


class TestNeverBreaksTheSession(HookContractTestCase):
    """A hook that fails must fail silently, never taking Claude Code with it."""

    def test_malformed_stdin_exits_zero(self):
        self.hook("prompt", "this is not json")

    def test_empty_stdin_exits_zero(self):
        self.hook("prompt", "")

    def test_unknown_event_name_exits_zero(self):
        self.hook("bogus-event", {"session_id": SID})

    def test_no_event_argument_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="{}", text=True, capture_output=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_payload_without_session_id_still_logs(self):
        self.hook("prompt", {"prompt": "hi"})
        self.assertEqual([r["session_id"] for r in self.records()],
                         ["unknown", "unknown"])

    def test_stop_for_a_session_never_seen_before(self):
        self.hook("stop", {"session_id": "ffffffff-never-registered"})
        self.assertEqual([r["event"] for r in self.records()],
                         ["session_start", "session_end"])

    def test_stop_on_a_log_with_undecodable_bytes(self):
        """handle_stop reads the whole log back; a bad byte must not escape.

        Anything can end up in the file -- a torn write, a crashed process, an
        editor saving in another encoding. 0x81 is not valid as a leading UTF-8
        byte and is unmapped in cp1252, so read_text() raises whichever of the
        two is the platform default.
        """
        self.hook("prompt", {"session_id": SID, "prompt": "hi"})
        log, = sorted(self.logs.glob("*.log"))
        with open(log, "ab") as f:
            f.write(b'{"event": "prompt", "text": "\x81\xff"}\n')

        self.hook("stop", {"session_id": SID})

    def test_log_directory_replaced_by_a_file(self):
        """Every handler starts with LOG_DIR.mkdir(); make that fail.

        A file where the directory should be is the portable way to make the
        log directory unusable -- unlike chmod, it denies writes on Windows
        too -- and it stands in for the real-world causes (a removed or
        permission-changed ~/.claude/logs, a full disk).
        """
        self.logs.parent.mkdir(parents=True, exist_ok=True)
        self.logs.write_text("not a directory")

        for event in ("prompt", "pre", "tool", "stop"):
            with self.subTest(event=event):
                self.hook(event, {"session_id": SID, "tool_name": "Read",
                                  "tool_input": {}, "tool_response": {}})
        self.assertEqual(self.logs.read_text(), "not a directory")

    def test_log_path_that_cannot_be_appended_to(self):
        """A registered log path that is a directory breaks _append's open()."""
        self.logs.mkdir(parents=True, exist_ok=True)
        blocked = self.logs / "2020-01-01_00-00-00_deadbeef.log"
        blocked.mkdir()
        (self.logs / ".session_map.json").write_text(
            json.dumps({SID: {"path": str(blocked)}}))

        self.hook("prompt", {"session_id": SID, "prompt": "hi"})
        self.hook("stop", {"session_id": SID})


class TestSessionIsolation(HookContractTestCase):

    def test_two_sessions_do_not_share_a_log(self):
        a, b = "aaaaaaaa-1111", "bbbbbbbb-2222"
        self.hook("prompt", {"session_id": a, "prompt": "first"})
        self.hook("prompt", {"session_id": b, "prompt": "second"})

        files = sorted(self.logs.glob("*.log"))
        self.assertEqual(len(files), 2, f"expected two log files, got {files}")
        for path in files:
            records = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
            ids = {r["session_id"] for r in records}
            self.assertEqual(len(ids), 1, f"{path.name} mixes sessions: {ids}")

    def test_a_session_keeps_one_log_across_many_events(self):
        for i in range(12):
            self.hook("pre", {"session_id": SID, "tool_name": "Read",
                              "tool_input": {"file_path": f"/f{i}.py"}})
        self.assertEqual(len(sorted(self.logs.glob("*.log"))), 1)
        self.assertEqual(len(self.records()), 13)  # session_start + 12


if __name__ == "__main__":
    unittest.main()
