"""Exercise the updater event loop without installing or starting a client."""
import os
import subprocess
import sys
import unittest


_SCRIPT = """
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
from installer import gui
import sys

class App(QApplication):
    def __init__(self, args):
        super().__init__(args)
        QTimer.singleShot(4000, lambda: self.exit(99))

class Worker(QObject):
    progress = Signal(str, int)
    finished_ok = Signal()
    failed = Signal(str)
    def __init__(self, **kwargs):
        super().__init__()
    def start(self):
        if sys.argv[1] == "success":
            self.finished_ok.emit()
        else:
            self.failed.emit("test failure")
    def wait(self, *args):
        return True

with (
    patch.object(gui, "QApplication", App),
    patch.object(gui, "_InstallWorker", Worker),
    patch.object(gui, "_log_update_failure"),
    patch.object(gui.operations, "_exe_is_locked", return_value=False),
    patch.object(gui.paths, "installed_exe_path", return_value=Mock(is_file=lambda: True)),
    patch.object(gui.subprocess, "Popen") as launched,
    patch.object(gui._SilentUpdateWindow, "PREP_MS", 0),
    patch.object(gui._SilentUpdateWindow, "ERROR_SECONDS", 1),
):
    code = gui.run_silent()
    assert code == 0, f"update card vetoed exit: {code}"
    assert launched.call_count == 1
    assert not any(w.isVisible() for w in QApplication.topLevelWidgets())
"""

class SilentUpdateTests(unittest.TestCase):
    def test_card_exits_after_success_and_failure(self):
        for outcome in ("success", "failure"):
            with self.subTest(outcome=outcome):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-c", _SCRIPT, outcome],
                    env=env, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ReleaseVersionTests(unittest.TestCase):
    def test_four_component_version_resource_and_updates(self):
        import ast
        from pathlib import Path
        from kapro_tun.core import updater
        source = Path(__file__).resolve().parents[2] / "KaproTUN.spec"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        assignments = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_version_tuple" for t in node.targets
            ):
                assignments.append(node)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "_version_tuple":
                    assignments.append(node)
        self.assertEqual(len(assignments), 2)
        code = compile(ast.Module(body=assignments, type_ignores=[]), str(source), "exec")
        for version, expected in (
            ("4.2.4", (4, 2, 4, 0)),
            ("4.2.4.1", (4, 2, 4, 1)),
        ):
            values = {"_version": version}
            exec(code, values)
            self.assertEqual(values["_version_tuple"], expected)
        self.assertTrue(updater.is_newer("4.2.4.1", "4.2.4"))
        self.assertFalse(updater.is_newer("4.2.4.1", "4.2.4.1"))
