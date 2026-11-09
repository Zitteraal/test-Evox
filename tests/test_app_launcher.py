import os
import unittest
from unittest import mock

from computer_control import app_launcher as al


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class OpenApplicationTests(unittest.TestCase):
    def setUp(self):
        self._timeout = al.VERIFY_TIMEOUT_S
        al.VERIFY_TIMEOUT_S = 0.3
        patcher = mock.patch.object(al.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # time.monotonic muss weiterlaufen, damit die Schleife endet
        self._ticks = iter(x * 0.1 for x in range(1, 1000))
        mono = mock.patch.object(al.time, "monotonic", lambda: next(self._ticks))
        mono.start()
        self.addCleanup(mono.stop)

    def tearDown(self):
        al.VERIFY_TIMEOUT_S = self._timeout

    def test_process_appears_is_verified(self):
        states = [{}, {1234: FakeProc(1234)}]
        calls = {"n": 0}

        def running(patterns, exact=False):
            calls["n"] += 1
            return states[0] if calls["n"] == 1 else states[1]

        with mock.patch.object(al, "_running", side_effect=running), \
                mock.patch.object(al, "_resolve_target", return_value=("exe", "notepad.exe")), \
                mock.patch.object(al, "_start") as start:
            result = al.open_application("notepad")
        start.assert_called_once_with("exe", "notepad.exe")
        self.assertTrue(result.ok)
        self.assertTrue(result.verified)

    def test_no_process_is_only_unverified_never_verified(self):
        with mock.patch.object(al, "_running", return_value={}), \
                mock.patch.object(al, "_resolve_target", return_value=("shell", "weirdapp")), \
                mock.patch.object(al, "_start"):
            result = al.open_application("weirdapp")
        self.assertTrue(result.ok)
        self.assertFalse(result.verified)
        self.assertEqual(result.status, "unverified")
        self.assertIn("no matching process", result.message)

    def test_already_running_is_reported_as_such(self):
        proc = {7: FakeProc(7)}
        with mock.patch.object(al, "_running", return_value=proc), \
                mock.patch.object(al, "_resolve_target", return_value=("exe", "x.exe")), \
                mock.patch.object(al, "_start"):
            result = al.open_application("notepad")
        self.assertTrue(result.verified)
        self.assertIn("already running", result.message)

    def test_start_failures_are_failures(self):
        with mock.patch.object(al, "_running", return_value={}), \
                mock.patch.object(al, "_resolve_target", return_value=("shell", "nothing")), \
                mock.patch.object(al, "_start", side_effect=FileNotFoundError()):
            self.assertFalse(al.open_application("nothing").ok)
        with mock.patch.object(al, "_running", return_value={}), \
                mock.patch.object(al, "_resolve_target", return_value=("shell", "x")), \
                mock.patch.object(al, "_start", side_effect=OSError("denied")):
            result = al.open_application("x")
        self.assertFalse(result.ok)
        self.assertIn("denied", result.message)

    def test_explorer_is_never_reported_as_verified(self):
        with mock.patch.object(al, "_running", return_value={1: FakeProc(1)}), \
                mock.patch.object(al, "_resolve_target", return_value=("exe", "explorer.exe")), \
                mock.patch.object(al, "_start"):
            result = al.open_application("explorer")
        self.assertTrue(result.ok)
        self.assertFalse(result.verified)

    def test_invalid_names_never_reach_the_launcher(self):
        with mock.patch.object(al, "_start") as start:
            for name in ("cmd", "a & b", "..\\x", ""):
                self.assertFalse(al.open_application(name).ok, name)
        start.assert_not_called()

    def test_alias_is_used_for_process_matching(self):
        seen = []

        def running(patterns, exact=False):
            seen.append(list(patterns))
            return {}

        with mock.patch.object(al, "_running", side_effect=running), \
                mock.patch.object(al, "_resolve_target", return_value=("exe", "Code.exe")) as resolve, \
                mock.patch.object(al, "_start"):
            al.open_application("Visual Studio Code")
        self.assertEqual(resolve.call_args.args[0], "code")
        self.assertIn(["code"], seen)


class CloseApplicationTests(unittest.TestCase):
    def test_not_running_is_a_failure(self):
        with mock.patch.object(al, "_running", return_value={}):
            result = al.close_application("notepad")
        self.assertFalse(result.ok)

    def test_protected_processes_are_never_touched(self):
        with mock.patch.object(al, "_running") as running, mock.patch.object(al, "_post_close_windows") as post:
            for name in ("explorer", "svchost", "winlogon", "python"):
                result = al.close_application(name, force=True)
                self.assertFalse(result.ok, name)
        running.assert_not_called()
        post.assert_not_called()

    def test_own_process_is_excluded(self):
        own = os.getpid()
        with mock.patch.object(al, "_running", return_value={own: FakeProc(own)}), \
                mock.patch.object(al, "_post_close_windows") as post:
            result = al.close_application("notepad")
        self.assertFalse(result.ok)
        post.assert_not_called()

    def test_graceful_close_is_verified(self):
        with mock.patch.object(al, "_running", return_value={10: FakeProc(10)}), \
                mock.patch.object(al, "_post_close_windows", return_value=1), \
                mock.patch.object(al, "_wait_gone", return_value=[]):
            result = al.close_application("notepad")
        self.assertTrue(result.ok)
        self.assertTrue(result.verified)

    def test_still_running_is_failure_and_nothing_is_forced(self):
        proc = FakeProc(10)
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", return_value=1), \
                mock.patch.object(al, "_wait_gone", return_value=[proc]):
            result = al.close_application("notepad")
        self.assertFalse(result.ok)
        self.assertIn("still running", result.message)
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    def test_no_window_without_force_is_failure(self):
        proc = FakeProc(10)
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", return_value=0):
            result = al.close_application("notepad")
        self.assertFalse(result.ok)
        self.assertIn("force", result.message)
        self.assertFalse(proc.terminated)

    def test_win32_unavailable_does_not_crash(self):
        proc = FakeProc(10)
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", side_effect=ImportError("no win32")):
            result = al.close_application("notepad")
        self.assertFalse(result.ok)

    def test_force_terminates_and_verifies(self):
        proc = FakeProc(10)
        waits = iter([[proc], []])  # nach WM_CLOSE noch da, nach terminate weg
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", return_value=1), \
                mock.patch.object(al, "_wait_gone", side_effect=lambda procs, timeout: next(waits)):
            result = al.close_application("notepad", force=True)
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)
        self.assertTrue(result.ok)
        self.assertTrue(result.verified)

    def test_force_kills_if_terminate_is_not_enough(self):
        proc = FakeProc(10)
        waits = iter([[proc], [proc], []])
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", return_value=1), \
                mock.patch.object(al, "_wait_gone", side_effect=lambda procs, timeout: next(waits)):
            result = al.close_application("notepad", force=True)
        self.assertTrue(proc.terminated and proc.killed)
        self.assertTrue(result.ok)

    def test_force_that_fails_is_reported(self):
        proc = FakeProc(10)
        waits = iter([[proc], [proc], [proc]])
        with mock.patch.object(al, "_running", return_value={10: proc}), \
                mock.patch.object(al, "_post_close_windows", return_value=1), \
                mock.patch.object(al, "_wait_gone", side_effect=lambda procs, timeout: next(waits)):
            result = al.close_application("notepad", force=True)
        self.assertFalse(result.ok)


class ShortcutAndVscodeTests(unittest.TestCase):
    def test_start_menu_shortcut_prefers_exact_and_skips_uninstallers(self):
        import tempfile
        from pathlib import Path

        base = Path(tempfile.mkdtemp())
        for name in ("Uninstall Discord.lnk", "Discord Beta Docs.lnk", "Discord.lnk"):
            (base / name).write_text("", encoding="utf-8")
        with mock.patch.object(al, "_start_menu_dirs", return_value=[base]):
            found = al._find_start_menu_shortcut("discord")
        self.assertEqual(found.name, "Discord.lnk")

    def test_vscode_not_found(self):
        with mock.patch.object(al, "find_vscode", return_value=None):
            from pathlib import Path
            self.assertFalse(al.open_in_vscode(Path("x")).ok)

    def test_cmd_metacharacters_are_refused_for_code_cmd(self):
        from pathlib import Path

        with mock.patch.object(al, "find_vscode", return_value=Path("C:/vs/bin/code.cmd")), \
                mock.patch.object(al.subprocess, "Popen") as popen:
            result = al.open_in_vscode(Path("C:/proj/a&b.txt"))
        self.assertFalse(result.ok)
        popen.assert_not_called()

    def test_vscode_exe_gets_arguments_without_shell(self):
        from pathlib import Path

        with mock.patch.object(al, "find_vscode", return_value=Path("C:/vs/Code.exe")), \
                mock.patch.object(al.subprocess, "Popen") as popen, \
                mock.patch.object(al, "_running", return_value={1: FakeProc(1)}):
            result = al.open_in_vscode(Path("C:/proj/a&b.txt"))
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(args[0][1], "-r")
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
