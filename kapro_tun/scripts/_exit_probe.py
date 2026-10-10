"""TEMPORARY: narrow down what breaks interpreter exit on Linux. Removed
before release."""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication

mode = sys.argv[1]
app = QApplication([])
seen = []


class Sink(QObject):
    def take(self, value) -> None:
        seen.append(value)


class Plain(QObject):
    done = Signal(object)


def pump(*objs) -> None:
    for _ in range(100):
        time.sleep(0.01)
        for o in objs:
            QCoreApplication.sendPostedEvents(o, 0)
        if seen:
            break


if mode == "plain-thread-emit":
    sink, src = Sink(), Plain()
    src.done.connect(sink.take)
    t = threading.Thread(target=lambda: src.done.emit(1), daemon=True)
    t.start()
    t.join()
    pump(sink)
elif mode == "plain-thread-emit-nodaemon":
    sink, src = Sink(), Plain()
    src.done.connect(sink.take)
    t = threading.Thread(target=lambda: src.done.emit(1))
    t.start()
    t.join()
    pump(sink)
elif mode == "background":
    from kapro_tun.gui import background

    class Job(background.Background):
        done = Signal(object)

        def _work(self):
            return lambda: self.done.emit(1)

    sink, job = Sink(), Job()
    job.done.connect(sink.take)
    job.start()
    pump(sink, job)
elif mode == "background-processevents":
    from kapro_tun.gui import background

    class Job(background.Background):
        done = Signal(object)

        def _work(self):
            return lambda: self.done.emit(1)

    sink, job = Sink(), Job()
    job.done.connect(sink.take)
    job.start()
    pump(sink, job)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
elif mode == "background-no-deletelater":
    from kapro_tun.gui import background
    background.Background._retire = lambda self: background._live.discard(self)

    class Job(background.Background):
        done = Signal(object)

        def _work(self):
            return lambda: self.done.emit(1)

    sink, job = Sink(), Job()
    job.done.connect(sink.take)
    job.start()
    pump(sink, job)
elif mode == "queued-main-only":
    from PySide6.QtCore import Qt
    sink, src = Sink(), Plain()
    src.done.connect(sink.take, Qt.QueuedConnection)
    src.done.emit(1)
    pump(sink)
elif mode == "deletelater-only":
    o = Plain()
    o.deleteLater()
elif mode == "window-shown":
    from PySide6.QtWidgets import QWidget
    w = QWidget()
    w.show()
    w.hide()
elif mode == "clipboard":
    QApplication.clipboard().setText("x")
elif mode == "overlay":
    from PySide6.QtWidgets import QWidget
    from kapro_tun.gui import kit
    host = QWidget()
    d = kit.OverlayDialog(host)
    d.head("trash", "t", "x")
    d.add_actions([("ok", "ok", "primary")], default="ok")
elif mode == "paste-dialog":
    from kapro_tun.gui import add_server_v2
    d = add_server_v2.PasteDialog(None)
elif mode == "add-page":
    from kapro_tun.gui import add_server_v2
    p = add_server_v2.AddServerPage()

print("probe", mode, "seen", seen, flush=True)
sys.exit(0)
