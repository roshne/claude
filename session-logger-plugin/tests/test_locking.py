#!/usr/bin/env python3
"""Tests for the file-locking shim in scripts/session-logger.py.

The concurrency tests spawn real subprocesses, because the races they cover
only exist between processes -- Claude Code runs each hook as its own process.

Run with:  python -m unittest discover session-logger-plugin/tests
Also runs under pytest, but uses only the standard library.

This file doubles as its own subprocess worker (see `worker_*` below), so it
must be importable and runnable as a script.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "session-logger.py"
IS_WINDOWS = sys.platform == "win32"

# Lead time before a synchronised burst, long enough for every child to have
# started its interpreter and reached the barrier.
BARRIER_LEAD = 2.0


# ---------------------------------------------------------------------------
# Loading the hook module
# ---------------------------------------------------------------------------

def load_logger(log_dir):
    """Import session-logger.py and point it at log_dir.

    The filename is hyphenated, so it cannot be imported by name. Rebinding
    the three module-level paths is enough to redirect it away from
    ~/.claude/logs: every function reads them at call time.
    """
    spec = importlib.util.spec_from_file_location("session_logger", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log_dir = pathlib.Path(log_dir)
    mod.LOG_DIR = log_dir
    mod.SESSION_MAP = log_dir / ".session_map.json"
    mod.SESSION_MAP_LOCK = log_dir / ".session_map.lock"
    return mod


def wait_for(barrier):
    """Spin until the shared start time, so children act simultaneously."""
    barrier = float(barrier)
    remaining = barrier - time.time()
    if remaining > 0.05:
        time.sleep(remaining - 0.05)
    while time.time() < barrier:
        pass


def spawn(*args):
    return subprocess.Popen(
        [sys.executable, str(pathlib.Path(__file__).resolve()), *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def drain(procs):
    """Wait for every child; return the list of non-empty stderr outputs."""
    failures = []
    for p in procs:
        _, err = p.communicate(timeout=60)
        if p.returncode != 0 or err.strip():
            failures.append(err.strip() or f"exit {p.returncode}")
    return failures


# ---------------------------------------------------------------------------
# Subprocess workers
# ---------------------------------------------------------------------------

def worker_register(log_dir, session_id, barrier):
    """Register one session, exactly as a real hook's first event would."""
    mod = load_logger(log_dir)
    wait_for(barrier)
    mod.get_or_create_log(session_id)


def worker_append(log_dir, log_path, tag, count, barrier):
    """Append `count` records to one shared log file."""
    mod = load_logger(log_dir)
    log = pathlib.Path(log_path)
    wait_for(barrier)
    for i in range(int(count)):
        mod._append(log, {"event": "tool_post", "worker": tag, "seq": i})


def worker_register_stalled(log_dir, session_id, stall):
    """Register, then stall in the gap between releasing the lock and writing
    the first record -- the window in which _prune could evict the entry."""
    mod = load_logger(log_dir)
    real_append = mod._append

    def slow_append(log, record):
        print("registered", flush=True)
        time.sleep(float(stall))
        return real_append(log, record)

    mod._append = slow_append
    mod.get_or_create_log(session_id)


def worker_hold(log_dir, path, seconds, mode):
    """Acquire the shim's lock on `path`, announce it, then hold it."""
    mod = load_logger(log_dir)
    with open(path, mode) as f:
        mod._lock_ex(f)
        print("locked", flush=True)
        time.sleep(float(seconds))
        mod._unlock(f)


WORKERS = {
    "register": worker_register,
    "register_stalled": worker_register_stalled,
    "append": worker_append,
    "hold": worker_hold,
}


def hold_lock(log_dir, path, seconds, mode="a"):
    """Start a holder child and block until it reports the lock acquired."""
    child = spawn("--worker", "hold", log_dir, path, seconds, mode)
    ack = child.stdout.readline().strip()
    if ack != "locked":
        raise AssertionError(f"holder failed to lock: {ack!r} {child.stderr.read()!r}")
    return child


def finish(child):
    """Wait for a holder child and close its pipes."""
    try:
        child.communicate(timeout=30)
    finally:
        for pipe in (child.stdout, child.stderr):
            if pipe and not pipe.closed:
                pipe.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class LockingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.mod = load_logger(self.dir)

    def tearDown(self):
        self._tmp.cleanup()


class TestBackendSelection(LockingTestCase):

    def test_platform_selects_a_real_lock_backend(self):
        expected = "msvcrt" if IS_WINDOWS else "fcntl"
        self.assertEqual(self.mod._LOCK_BACKEND, expected)

    def test_no_op_backend_is_not_used_on_supported_platforms(self):
        self.assertNotEqual(self.mod._LOCK_BACKEND, "none")


class TestSessionMapConcurrency(LockingTestCase):
    """The race issue #2 is about: simultaneous session registration."""

    N = 8

    def test_concurrent_registration_loses_no_sessions(self):
        barrier = time.time() + BARRIER_LEAD
        ids = [f"{i:08d}" for i in range(self.N)]  # distinct in the first 8 chars
        procs = [spawn("--worker", "register", self.dir, sid, barrier) for sid in ids]
        self.assertEqual(drain(procs), [], "a worker failed")

        m = json.loads((pathlib.Path(self.dir) / ".session_map.json").read_text())
        missing = sorted(set(ids) - set(m))
        self.assertEqual(missing, [], f"lost {len(missing)}/{self.N} registrations")

    def test_a_registration_is_not_pruned_before_its_first_record(self):
        """The log file must be created inside the lock, not by the later
        _append(). _prune() drops entries whose file is missing, so a
        registration published without its file can be evicted by the next
        process to take the lock -- the evicted session then registers again
        and its log splits across two files.

        The window is only ~100us wide in practice, so this test forces the
        interleaving instead of racing for it.
        """
        child = spawn("--worker", "register_stalled", self.dir, "aaaaaaaa", "2.0")
        try:
            ack = child.stdout.readline().strip()
            if ack != "registered":
                # Read stderr only on failure: it blocks until the child exits,
                # which would let the stall finish and hide the very race this
                # test exists to catch.
                self.fail(f"worker did not register: {ack!r} {child.stderr.read()}")
            # A is registered but has written nothing yet. Registering B runs
            # _prune over A's entry.
            self.mod.get_or_create_log("bbbbbbbb")
            m = json.loads((pathlib.Path(self.dir) / ".session_map.json").read_text())
            self.assertIn("aaaaaaaa", m, "a just-registered session was pruned")
            self.assertIn("bbbbbbbb", m)
        finally:
            finish(child)

        m = json.loads((pathlib.Path(self.dir) / ".session_map.json").read_text())
        self.assertEqual(sorted(m), ["aaaaaaaa", "bbbbbbbb"])
        logs = sorted(pathlib.Path(self.dir).glob("*.log"))
        self.assertEqual(len(logs), 2, f"session logs fragmented: {logs}")

    def test_each_session_gets_its_own_log_with_one_start_record(self):
        barrier = time.time() + BARRIER_LEAD
        ids = [f"{i:08d}" for i in range(self.N)]
        procs = [spawn("--worker", "register", self.dir, sid, barrier) for sid in ids]
        self.assertEqual(drain(procs), [], "a worker failed")

        m = json.loads((pathlib.Path(self.dir) / ".session_map.json").read_text())
        self.assertEqual(len(m), self.N, f"lost {self.N - len(m)}/{self.N} registrations")
        paths = [info["path"] for info in m.values()]
        self.assertEqual(len(set(paths)), self.N, "sessions shared a log file")
        for sid, info in m.items():
            records = [json.loads(x) for x in
                       pathlib.Path(info["path"]).read_text().splitlines() if x.strip()]
            starts = [r for r in records if r.get("event") == "session_start"]
            self.assertEqual(len(starts), 1, f"session {sid}: {len(starts)} start records")
            self.assertEqual(starts[0]["session_id"], sid)


class TestAppendConcurrency(LockingTestCase):

    WORKERS = 6
    PER_WORKER = 25

    def test_concurrent_appends_are_complete_and_uncorrupted(self):
        log = pathlib.Path(self.dir) / "shared.log"
        log.write_text("")
        barrier = time.time() + BARRIER_LEAD
        procs = [
            spawn("--worker", "append", self.dir, log, f"w{i}", self.PER_WORKER, barrier)
            for i in range(self.WORKERS)
        ]
        self.assertEqual(drain(procs), [], "a worker failed")

        lines = [x for x in log.read_text().splitlines() if x.strip()]
        self.assertEqual(len(lines), self.WORKERS * self.PER_WORKER)

        seen = set()
        for i, raw in enumerate(lines):
            try:
                r = json.loads(raw)
            except json.JSONDecodeError as e:
                self.fail(f"line {i} is not valid JSON ({e}): {raw!r}")
            seen.add((r["worker"], r["seq"]))
        expected = {(f"w{i}", s)
                    for i in range(self.WORKERS) for s in range(self.PER_WORKER)}
        self.assertEqual(seen, expected, "records were lost or duplicated")

    def test_append_flushes_inside_the_lock(self):
        """The bytes must be on disk before the lock is released.

        f.write() only fills the buffer; without an explicit flush the `with`
        block flushes at close(), after _unlock(), so the lock protects
        nothing.
        """
        log = pathlib.Path(self.dir) / "flush.log"
        log.write_text("")
        sizes = []
        real_unlock = self.mod._unlock

        def spy(f):
            sizes.append(os.path.getsize(log))
            return real_unlock(f)

        self.mod._unlock = spy
        try:
            self.mod._append(log, {"event": "prompt", "text": "hello"})
        finally:
            self.mod._unlock = real_unlock

        self.assertEqual(len(sizes), 1, "_unlock was not called exactly once")
        self.assertGreater(sizes[0], 0, "record was still buffered when the lock dropped")

    def test_append_does_not_create_a_sparse_file(self):
        """The msvcrt backend seeks to a 1 TiB offset; O_APPEND must ignore it.

        If the seek leaked into the write position the file would report a
        size past _LOCK_OFFSET and be padded with NULs.
        """
        log = pathlib.Path(self.dir) / "sparse.log"
        log.write_text("")
        for i in range(5):
            self.mod._append(log, {"event": "tool_post", "seq": i})

        raw = log.read_bytes()
        self.assertLess(os.path.getsize(log), 4096)
        self.assertNotIn(b"\x00", raw)
        self.assertTrue(raw.startswith(b"{"), f"log starts with {raw[:16]!r}")
        records = [json.loads(x) for x in raw.decode().splitlines() if x.strip()]
        self.assertEqual([r["seq"] for r in records], list(range(5)))


class TestLockDoesNotBreakReaders(LockingTestCase):
    """Windows range locks are mandatory: a badly placed lock breaks readers."""

    def test_unlocked_reader_succeeds_while_the_lock_is_held(self):
        log = pathlib.Path(self.dir) / "held.log"
        log.write_text('{"event": "prompt"}\n' * 50)
        child = hold_lock(self.dir, log, 3.0)
        try:
            # This is exactly what handle_stop() does, with no lock of its own.
            lines = [x for x in log.read_text().splitlines() if x.strip()]
            self.assertEqual(len(lines), 50)
            self.assertGreater(os.path.getsize(log), 0)
        finally:
            finish(child)

    def test_handle_stop_works_while_another_process_holds_the_lock(self):
        log_dir = pathlib.Path(self.dir)
        session = "abcdef01"
        self.mod.get_or_create_log(session)
        log = pathlib.Path(json.loads((log_dir / ".session_map.json").read_text())
                           [session]["path"])
        for i in range(10):
            self.mod._append(log, {"event": "tool_post", "status": "ok", "seq": i})

        child = hold_lock(self.dir, log, 3.0)
        try:
            self.mod.handle_stop({"session_id": session})
        finally:
            finish(child)

        records = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
        ends = [r for r in records if r.get("event") == "session_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["stats"]["tools_completed"], 10)


class TestFailureModes(LockingTestCase):

    def test_unlock_without_a_held_lock_does_not_raise(self):
        p = pathlib.Path(self.dir) / "never-locked"
        with open(p, "w") as f:
            self.mod._unlock(f)  # must not raise

    def test_unlock_is_idempotent(self):
        p = pathlib.Path(self.dir) / "double-unlock"
        with open(p, "w") as f:
            self.mod._lock_ex(f)
            self.mod._unlock(f)
            self.mod._unlock(f)  # must not raise

    @unittest.skipUnless(IS_WINDOWS, "POSIX flock() blocks indefinitely by design")
    def test_contended_lock_times_out_instead_of_raising(self):
        """A hook must never crash or stall for long on a stuck lock."""
        p = pathlib.Path(self.dir) / "contended"
        p.write_text("")
        # The holder must outlive the assertion bound, or an acquire that
        # simply waits for the release would pass: hold 4s, allow 3s, deadline
        # 2s. That leaves a full second of margin on each side rather than
        # relying on clock jitter to tell "timed out" from "waited it out".
        hold = self.mod._LOCK_TIMEOUT + 2.0
        limit = self.mod._LOCK_TIMEOUT + 1.0
        child = hold_lock(self.dir, p, hold)
        try:
            with open(p, "a") as f:
                t0 = time.monotonic()
                self.mod._lock_ex(f)  # must return, not raise
                elapsed = time.monotonic() - t0
                self.mod._unlock(f)   # must not raise despite never acquiring
        finally:
            finish(child)
        self.assertGreaterEqual(elapsed, self.mod._LOCK_TIMEOUT * 0.5)
        self.assertLess(elapsed, limit, "acquire blocked past its deadline")

    @unittest.skipUnless(IS_WINDOWS, "msvcrt-specific")
    def test_second_process_is_genuinely_excluded(self):
        import msvcrt
        p = pathlib.Path(self.dir) / "excluded"
        p.write_text("")
        child = hold_lock(self.dir, p, 3.0)
        try:
            with open(p, "a") as f:
                f.seek(self.mod._LOCK_OFFSET)
                with self.assertRaises(OSError):
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        finally:
            finish(child)

    @unittest.skipUnless(IS_WINDOWS, "msvcrt-specific")
    def test_truncating_open_succeeds_while_the_lock_is_held(self):
        """_update_session_map opens the lock file with "w", which truncates."""
        p = pathlib.Path(self.dir) / "trunc.lock"
        p.write_text("")
        child = hold_lock(self.dir, p, 3.0, mode="w")
        try:
            with open(p, "w"):
                pass  # must not raise a lock violation
        finally:
            finish(child)


# ---------------------------------------------------------------------------
# Entry point: test runner, or subprocess worker
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        WORKERS[sys.argv[2]](*sys.argv[3:])
    else:
        unittest.main()
