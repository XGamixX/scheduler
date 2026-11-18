#!/usr/bin/env python3
"""Tests for the optional startup keyboard preflight."""

from __future__ import annotations

import io
import sys
import time
import types
import unittest
from unittest.mock import patch

import keystroke


class FakeKey:
    def __init__(self, name: str):
        self.name = name


class FakeListener:
    def __init__(self, on_press):
        self.on_press = on_press
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def stop(self):
        self.alive = False

    def join(self, timeout=None):
        return None


class FakePyAutoGUI:
    def __init__(self, listener_getter, dropped_calls=()):
        self.listener_getter = listener_getter
        self.dropped_calls = set(dropped_calls)
        self.keys = []

    def press(self, key):
        self.keys.append(key)
        call_number = len(self.keys)
        listener = self.listener_getter()
        if call_number not in self.dropped_calls and listener is not None:
            listener.on_press(FakeKey(key))


class StartupKeyboardCheckTests(unittest.TestCase):
    def setUp(self):
        self.listeners = []

        def make_listener(on_press):
            listener = FakeListener(on_press)
            self.listeners.append(listener)
            return listener

        keyboard_module = types.SimpleNamespace(Listener=make_listener)
        pynput_module = types.SimpleNamespace(keyboard=keyboard_module)
        self.pynput_patch = patch.dict(sys.modules, {"pynput": pynput_module})
        self.pynput_patch.start()
        self.addCleanup(self.pynput_patch.stop)

        self.stdin_patch = patch.object(keystroke.sys, "stdin", io.StringIO(""))
        self.stdin_patch.start()
        self.addCleanup(self.stdin_patch.stop)

        self.isatty_patch = patch.object(keystroke.sys.stdin, "isatty", return_value=False)
        self.isatty_patch.start()
        self.addCleanup(self.isatty_patch.stop)

        self.first_grace_patch = patch.object(keystroke, "STARTUP_FIRST_GRACE_SECONDS", 0.04)
        self.second_grace_patch = patch.object(keystroke, "STARTUP_SECOND_GRACE_SECONDS", 0.02)
        self.first_grace_patch.start()
        self.second_grace_patch.start()
        self.addCleanup(self.first_grace_patch.stop)
        self.addCleanup(self.second_grace_patch.stop)

    def _listener(self):
        return self.listeners[-1] if self.listeners else None

    def test_both_probes_are_captured(self):
        fake_pyautogui = FakePyAutoGUI(self._listener)
        with patch.object(keystroke, "pyautogui", fake_pyautogui):
            self.assertTrue(keystroke._startup_keyboard_check())
        self.assertEqual(fake_pyautogui.keys, ["enter", "enter"])

    def test_second_probe_can_succeed_when_first_is_lost(self):
        fake_pyautogui = FakePyAutoGUI(self._listener, dropped_calls={1})
        with patch.object(keystroke, "pyautogui", fake_pyautogui):
            self.assertTrue(keystroke._startup_keyboard_check())
        self.assertEqual(fake_pyautogui.keys, ["enter", "enter"])

    def test_failure_warns_but_returns_normally(self):
        fake_pyautogui = FakePyAutoGUI(self._listener, dropped_calls={1, 2})
        output = io.StringIO()
        with patch.object(keystroke, "pyautogui", fake_pyautogui), patch(
            "builtins.print", side_effect=lambda *a, **k: output.write(" ".join(map(str, a)) + "\\n")
        ):
            self.assertFalse(keystroke._startup_keyboard_check())
        self.assertIn("WARNING: STARTUP KEYBOARD TEST FAILED", output.getvalue())

    def test_failed_probes_each_reach_their_timeout_and_then_cancel(self):
        fake_pyautogui = FakePyAutoGUI(self._listener, dropped_calls={1, 2})
        started = time.monotonic()
        with patch.object(keystroke, "pyautogui", fake_pyautogui):
            self.assertFalse(keystroke._startup_keyboard_check())
        elapsed = time.monotonic() - started

        # Both independent waits must expire (40 ms + 20 ms in this test),
        # and the function must return promptly instead of leaving a listener
        # or an unbounded queue wait running.
        self.assertGreaterEqual(elapsed, 0.055)
        self.assertLess(elapsed, 0.5)

    def test_startup_preflight_is_enabled_by_default(self):
        args = keystroke._build_parser().parse_args([])
        self.assertTrue(args.startup_key_test)

    def test_cli_flag_disables_preflight(self):
        args = keystroke._build_parser().parse_args(["--no-startup-key-test"])
        self.assertFalse(args.startup_key_test)
        args_alias = keystroke._build_parser().parse_args(["--skip-startup-key-test"])
        self.assertFalse(args_alias.startup_key_test)


if __name__ == "__main__":
    unittest.main(verbosity=2)
