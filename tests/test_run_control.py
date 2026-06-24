"""Tests for openfde.run_control — bounded + externally cancellable managed subprocesses.

This is the safety primitive the Program relay was missing: a hung provider call must die on cancel
(not at its wall-clock timeout) and surface a DISTINCT outcome (cancelled vs timed-out)."""

import threading
import time
import unittest

from openfde import run_control


class RunControlTest(unittest.TestCase):
    def tearDown(self):
        for rid in ("rc_ok", "rc_cancel", "rc_timeout", "rc_pre"):
            run_control.reset(rid)

    def test_completes_normally_and_returns_output(self):
        r = run_control.run_managed(["printf", "hello"], run_id="rc_ok", provider="echo",
                                    role="architect", phase="plan", timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout)
        self.assertFalse(run_control.is_cancelled("rc_ok"))

    def test_request_cancel_kills_live_subprocess_promptly(self):
        result = {}

        def go():
            try:
                run_control.run_managed(["sleep", "30"], run_id="rc_cancel", provider="claude-code",
                                        role="architect", phase="plan", timeout=120)
            except run_control.ProviderCancelled as exc:
                result["cancelled"], result["role"] = exc, exc.role

        t = threading.Thread(target=go)
        t.start()
        time.sleep(0.6)                                  # let it spawn + register
        started = time.monotonic()
        killed = run_control.request_cancel("rc_cancel")
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "managed call did not return after cancel")
        self.assertGreaterEqual(killed, 1)               # a live subprocess was signalled
        self.assertIn("cancelled", result)               # raised ProviderCancelled, not timeout
        self.assertEqual(result["role"], "architect")
        self.assertLess(time.monotonic() - started, 8)   # died promptly, not at the 120s budget

    def test_timeout_raises_provider_timeout(self):
        start = time.monotonic()
        with self.assertRaises(run_control.ProviderTimeout) as cm:
            run_control.run_managed(["sleep", "30"], run_id="rc_timeout", provider="codex",
                                    role="verifier", phase="verify", timeout=1)
        self.assertEqual(cm.exception.role, "verifier")
        self.assertEqual(cm.exception.seconds, 1)
        self.assertLess(time.monotonic() - start, 5)     # killed at the deadline, not after 30s

    def test_cancel_before_spawn_aborts_immediately(self):
        run_control.request_cancel("rc_pre")             # flag set before any call
        with self.assertRaises(run_control.ProviderCancelled):
            run_control.run_managed(["sleep", "30"], run_id="rc_pre", provider="codex",
                                    role="architect", phase="plan", timeout=30)


if __name__ == "__main__":
    unittest.main()
