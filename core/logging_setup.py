"""
Zentrales Logging: Datei (rotierend) + Konsole, falls vorhanden.
Unbehandelte Fehler (auch aus Threads) landen ebenfalls im Log.
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import settings

_configured = False


def setup_logging(level: int = logging.INFO) -> Path:
    """Richtet das Logging einmalig ein und gibt den Pfad der Logdatei zurück."""
    global _configured
    log_file = settings.LOGS_DIR / "evox.log"
    if _configured:
        return log_file

    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Ohne Konsole (z. B. EXE ohne Fenster) ist sys.stderr None.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    logging.captureWarnings(True)

    def _log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("evox.uncaught").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _log_thread_exception(args):
        logging.getLogger("evox.uncaught").critical(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_exception

    _configured = True
    return log_file
