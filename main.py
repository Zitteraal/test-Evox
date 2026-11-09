"""
EVOX – Personal Intelligence Workspace
Einstiegspunkt. Startet die Qt/QML-Anwendung.
"""
import sys
import os
import ctypes
import logging
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtCore import QUrl

from config import settings
from core.logging_setup import setup_logging

log = logging.getLogger("evox.main")


def _log_qml_warnings(warnings) -> None:
    for w in warnings:
        log.warning("QML: %s", w.toString())


def main() -> int:
    # 0. LOGGING ZUERST – alles danach (auch Fehler) landet in %LOCALAPPDATA%\EVOX\Logs\evox.log
    log_file = setup_logging()
    log.info("EVOX %s startet (Programm: %s, Daten: %s)", settings.APP_VERSION, settings.BASE_DIR, settings.USER_DATA_DIR)
    if settings.LEGACY_DB_MIGRATED:
        log.info("Alte Datenbank wurde nach %s kopiert (das Original bleibt erhalten).", settings.DB_PATH)

    # 1. WINDOWS APP ID (Verhindert das Python-Logo in der Taskleiste)
    try:
        myappid = "EVOX.PersonalIntelligenceWorkspace"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception as e:
        log.warning("AppUserModelID konnte nicht gesetzt werden: %s", e)

    # QtQuick Controls im "Basic"-Stil
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

    app = QGuiApplication(sys.argv)
    app.setApplicationName(settings.APP_NAME)
    app.setOrganizationName("EVOX Systems")
    app.setApplicationVersion(settings.APP_VERSION)

    # 2. DAS EVOX ICON LADEN
    base_dir = settings.BASE_DIR
    icon_path = base_dir / "assets" / "evox_app_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        log.warning("EVOX Icon nicht gefunden unter %s", icon_path)

    # Backend erst jetzt importieren/erzeugen, damit Importfehler im Log landen
    from interface.backend import Backend

    backend = Backend()

    engine = QQmlApplicationEngine()
    engine.warnings.connect(_log_qml_warnings)

    # QML-Importpfade
    qml_dir = base_dir / "interface" / "qml"
    engine.addImportPath(str(qml_dir))

    # Backend als globales Objekt in QML verfügbar machen
    engine.rootContext().setContextProperty("backend", backend)

    # Hauptfenster laden
    engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))

    if not engine.rootObjects():
        log.critical("QML konnte nicht geladen werden (Details siehe Warnungen oben, Log: %s).", log_file)
        return -1

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
