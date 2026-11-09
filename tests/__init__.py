"""
Gemeinsame Test-Einrichtung (wird vor jedem Test importiert, egal ob pytest oder unittest).

- Alle EVOX-Daten und Projekte landen in einem temporären Ordner, nie im echten Benutzerordner.
- Fehlen PySide6/httpx (z. B. auf einem Build-Server), werden minimale Ersatzmodule eingesetzt,
  damit die Logik trotzdem getestet werden kann.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_ROOT = Path(tempfile.mkdtemp(prefix="evox_test_"))
os.environ["EVOX_HOME"] = str(TEST_ROOT / "evox_home")
os.environ["EVOX_PROJECTS_DIR"] = str(TEST_ROOT / "projects")


def _install_qt_stub() -> None:
    class _BoundSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *args):
            for slot in list(self._slots):
                slot(*args)

    class Signal:
        def __init__(self, *types_):
            self._name = None

        def __set_name__(self, owner, name):
            self._name = "_sig_" + name

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            if self._name not in obj.__dict__:
                obj.__dict__[self._name] = _BoundSignal()
            return obj.__dict__[self._name]

    class QObject:
        def __init__(self, *args, **kwargs):
            pass

    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QObject = QObject
    qtcore.Signal = Signal
    pyside = types.ModuleType("PySide6")
    pyside.QtCore = qtcore
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore


try:
    import PySide6.QtCore  # noqa: F401
except ImportError:
    _install_qt_stub()

try:
    import httpx  # noqa: F401
except ImportError:
    _httpx = types.ModuleType("httpx")
    _httpx.Client = object
    sys.modules["httpx"] = _httpx
