"""Windows startup regressions; no real UAC, child processes or network changes.

Run with python -m unittest kapro_tun.scripts.test_windows_startup.
"""
import ast
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kapro_tun.core import admin, win_tools


class WindowsStartupTests(unittest.TestCase):
    def setUp(self):
        self.no_processes = patch.object(
            subprocess, "Popen", side_effect=AssertionError("real process forbidden"),
        )
        self.no_processes.start()
        self.addCleanup(self.no_processes.stop)

    def test_already_elevated_does_not_relaunch(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(admin, "is_admin", return_value=True), \
                patch.object(admin, "relaunch_as_admin") as launch:
            self.assertIsNone(admin.require_windows_admin())
            launch.assert_not_called()

    def test_other_platforms_do_not_request_elevation(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), \
                    patch.object(sys, "platform", platform), \
                    patch.object(admin, "is_admin") as check:
                self.assertIsNone(admin.require_windows_admin())
                check.assert_not_called()

    def test_successful_elevation_exits_original(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(admin, "is_admin", return_value=False), \
                patch.object(admin, "relaunch_as_admin", return_value=33):
            self.assertEqual(admin.require_windows_admin(), 0)

    def test_cancel_or_failure_exits_with_message(self):
        for result in (0, 5, 32, OSError("denied")):
            shell = Mock()
            with self.subTest(result=result), \
                    patch.object(sys, "platform", "win32"), \
                    patch.object(admin, "is_admin", return_value=False), \
                    patch.object(admin.ctypes, "windll", shell, create=True), \
                    patch.object(admin, "relaunch_as_admin") as launch:
                if isinstance(result, Exception):
                    launch.side_effect = result
                else:
                    launch.return_value = result
                self.assertEqual(admin.require_windows_admin(), 1)
                shell.user32.MessageBoxW.assert_called_once()

    def test_frozen_relaunch_preserves_arguments_without_duplicate_exe(self):
        shell = Mock()
        shell.shell32.ShellExecuteW.return_value = 42
        args = [r'C:\Program Files\KaproTUN.exe', '--minimized', 'a"b', 'trailing\\']
        with patch.object(admin.ctypes, "windll", shell, create=True), \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", args[0]):
            self.assertEqual(admin._win_relaunch(args), 42)
        call = shell.shell32.ShellExecuteW.call_args.args
        self.assertEqual(call[1:4], ("runas", args[0], subprocess.list2cmdline(args[1:])))

    def test_development_relaunch_uses_pythonw(self):
        shell = Mock()
        shell.shell32.ShellExecuteW.return_value = 42
        with patch.object(admin.ctypes, "windll", shell, create=True), \
                patch.object(sys, "frozen", False, create=True), \
                patch.object(sys, "executable", str(Path('venv') / 'python.exe')), \
                patch.object(admin.os.path, "isfile", return_value=True):
            admin._win_relaunch(['run.py', '--minimized'])
        call = shell.shell32.ShellExecuteW.call_args.args
        self.assertEqual(Path(call[2]).name, 'pythonw.exe')
        self.assertEqual(call[3], 'run.py --minimized')

    def test_helper_windows_explicitly_hidden(self):
        info = Mock(dwFlags=0)
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(subprocess, 'STARTUPINFO', return_value=info, create=True), \
                patch.object(subprocess, 'STARTF_USESHOWWINDOW', 1, create=True), \
                patch.object(subprocess, 'SW_HIDE', 0, create=True):
            self.assertIs(win_tools.hidden_startupinfo(), info)
        self.assertEqual(info.dwFlags & 1, 1)
        self.assertEqual(info.wShowWindow, 0)
        with patch.object(sys, 'platform', 'linux'):
            self.assertIsNone(win_tools.hidden_startupinfo())

    def test_main_never_starts_gui_or_cleanup_when_relaunching(self):
        from kapro_tun import main
        for code in (0, 1):
            with self.subTest(code=code), patch.object(sys, 'argv', ['KaproTUN.exe']), \
                    patch.object(admin, 'require_windows_admin', return_value=code), \
                    patch.object(main, '_run_app') as run:
                self.assertEqual(main.main(), code)
                run.assert_not_called()

    def test_emulated_windows_without_windows_subprocess_api(self):
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(subprocess, 'STARTUPINFO', None, create=True):
            self.assertIsNone(win_tools.hidden_startupinfo())

    def test_windows_helpers_keep_both_window_controls(self):
        root = Path(__file__).resolve().parents[1]
        checked = 0
        for path in [root / 'main.py', *(root / 'core').glob('*.py')]:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not isinstance(node, ast.Call):
                    continue
                flags = next((k.value for k in node.keywords if k.arg == 'creationflags'), None)
                if flags is None or 'CREATE_NO_WINDOW' not in ast.unparse(flags) and '_NO_WINDOW' not in ast.unparse(flags):
                    continue
                self.assertTrue(any(k.arg == 'startupinfo' for k in node.keywords),
                                f'{path.name}:{node.lineno} can show a helper window')
                checked += 1
        self.assertGreater(checked, 10)

    def test_windowed_selftest_exits_without_stdout_or_startup_cleanup(self):
        from kapro_tun import main, selftest
        with patch.object(sys, 'argv', ['KaproTUN.exe', '--selftest']), \
                patch.object(sys, 'stdout', None), \
                patch.object(selftest, 'run', return_value=0), \
                patch.object(main.os, '_exit', side_effect=SystemExit(0)) as exit_now, \
                patch.object(main, '_run_app') as run:
            with self.assertRaises(SystemExit):
                main.main()
            exit_now.assert_called_once_with(0)
            run.assert_not_called()

    def test_powershell_explicitly_hides_its_window(self):
        root = Path(__file__).resolve().parents[1]
        checked = 0
        for path in [root / 'main.py', *(root / 'core').glob('*.py')]:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not isinstance(node, ast.List) or not node.elts:
                    continue
                if ast.unparse(node.elts[0]) != "win_tools.tool('powershell')":
                    continue
                values = [n.value if isinstance(n, ast.Constant) else None for n in node.elts]
                self.assertIn('-WindowStyle', values, f'{path.name}:{node.lineno}')
                self.assertEqual(values[values.index('-WindowStyle') + 1], 'Hidden')
                checked += 1
        self.assertGreaterEqual(checked, 4)


if __name__ == '__main__':
    unittest.main()
