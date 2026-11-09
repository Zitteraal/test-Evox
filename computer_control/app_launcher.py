"""
Programme starten, schließen und in VS Code öffnen – ohne Shell und mit Erfolgsprüfung.

Unterschiede zu vorher:
  - kein `shell=True`, keine Zeichenketten aus dem Modell in einer Shell
  - Namen werden validiert (core.security.validate_app_name)
  - "Erfolg" gibt es nur, wenn ein passender Prozess tatsächlich läuft;
    sonst lautet das Ergebnis "unverified" bzw. "failed"
  - close_application ist jetzt wirklich implementiert (erst sauber schließen,
    hartes Beenden nur mit force=True)
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from core import security
from core.tool_result import ToolResult

log = logging.getLogger("evox.apps")

VERIFY_TIMEOUT_S = 5.0     # so lange wird nach dem Start auf den Prozess gewartet
CLOSE_TIMEOUT_S = 5.0      # so lange wird nach WM_CLOSE auf das Ende gewartet

# Anzeige-/Alltagsnamen -> Startziel
APP_ALIASES = {
    "vscode": "code", "vs code": "code", "visual studio code": "code", "code": "code",
    "notepad": "notepad", "editor": "notepad",
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox",
    "explorer": "explorer", "file explorer": "explorer", "datei-explorer": "explorer",
    "calculator": "calc", "rechner": "calc", "taschenrechner": "calc",
    "paint": "mspaint",
    "task manager": "taskmgr", "taskmanager": "taskmgr",
}

# Startziel -> mögliche Prozessnamen (ohne .exe)
PROCESS_NAMES = {
    "code": ["code"],
    "notepad": ["notepad"],
    "chrome": ["chrome"],
    "msedge": ["msedge"],
    "firefox": ["firefox"],
    "explorer": ["explorer"],
    "calc": ["calculatorapp", "calc", "win32calc", "calculator"],
    "mspaint": ["mspaint", "paintstudio.view"],
    "taskmgr": ["taskmgr"],
}

# Prozesse, deren Vorhandensein nichts über einen erfolgreichen Start aussagt
_ALWAYS_RUNNING = {"explorer"}

# Prozesse, die EVOX niemals schließt
PROTECTED_PROCESSES = {
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass",
    "svchost", "dwm", "explorer", "fontdrvhost", "sihost", "taskhostw", "ctfmon",
    "python", "pythonw", "evox",
}

_SKIP_SHORTCUT_WORDS = ("uninstall", "deinstall", "remove", "readme", "help", "documentation", "website")


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def _normalize(name: str) -> str:
    return " ".join(name.lower().replace(".exe", "").split())


def _stem(process_name: str) -> str:
    n = (process_name or "").lower()
    return n[:-4] if n.endswith(".exe") else n


def _running(patterns: list[str], exact: bool = False) -> dict[int, psutil.Process]:
    """Laufende Prozesse, deren Name zu einem Muster passt."""
    found: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            stem = _stem(proc.info.get("name") or "")
        except (psutil.Error, KeyError):
            continue
        for p in patterns:
            if stem == p or (not exact and len(p) >= 4 and p in stem):
                found[proc.info["pid"]] = proc
                break
    return found


def _own_pids() -> set[int]:
    pids = {os.getpid()}
    try:
        pids.update(p.pid for p in psutil.Process().parents())
    except psutil.Error:
        pass
    return pids


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for var in ("APPDATA", "ProgramData"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def _find_start_menu_shortcut(name: str) -> Path | None:
    """Sucht eine Startmenü-Verknüpfung (z. B. 'Discord.lnk')."""
    wanted = _normalize(name)
    best: tuple[int, Path] | None = None
    for directory in _start_menu_dirs():
        try:
            for lnk in directory.rglob("*.lnk"):
                stem = _normalize(lnk.stem)
                if any(w in stem for w in _SKIP_SHORTCUT_WORDS):
                    continue
                if stem == wanted:
                    return lnk
                if wanted in stem and (best is None or len(stem) < best[0]):
                    best = (len(stem), lnk)
        except OSError:
            continue
    return best[1] if best else None


def find_vscode() -> Path | None:
    """Findet Code.exe (bevorzugt) oder code.cmd."""
    cmd = shutil.which("code")
    if cmd:
        exe = Path(cmd).resolve().parent.parent / "Code.exe"
        if exe.exists():
            return exe
    candidates = []
    for var, sub in (("LOCALAPPDATA", r"Programs\Microsoft VS Code"), ("ProgramFiles", "Microsoft VS Code")):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / sub / "Code.exe")
    for c in candidates:
        if c.exists():
            return c
    return Path(cmd) if cmd else None


_CMD_METACHARS = set('&|<>^%!"')


def _start(kind: str, target: str) -> None:
    """Startet ein Ziel ohne Shell. Wird in Tests ersetzt."""
    if kind == "exe":
        try:
            subprocess.Popen(
                [target], close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) != 740:  # 740 = Rechteerhöhung nötig
                raise
    if not hasattr(os, "startfile"):
        raise OSError("Starting applications is only supported on Windows.")
    os.startfile(target)  # noqa: S606 - ShellExecute, keine cmd.exe


def _resolve_target(alias: str, original: str) -> tuple[str, str]:
    if alias == "code":
        vs = find_vscode()
        if vs:
            return ("exe", str(vs))
    exe = shutil.which(alias)
    if exe and exe.lower().endswith((".exe", ".com")):
        return ("exe", exe)
    lnk = _find_start_menu_shortcut(original)
    if lnk:
        return ("shortcut", str(lnk))
    return ("shell", alias)


# --------------------------------------------------------------------------- #
# Programm öffnen
# --------------------------------------------------------------------------- #
def open_application(app_name: str) -> ToolResult:
    try:
        name = security.validate_app_name(app_name)
    except ValueError as exc:
        return ToolResult.failure(str(exc))

    key = _normalize(name)
    alias = APP_ALIASES.get(key, key)
    patterns = PROCESS_NAMES.get(alias, [alias.replace(" ", "")])
    before = set(_running(patterns))

    try:
        kind, target = _resolve_target(alias, name)
        _start(kind, target)
    except FileNotFoundError:
        return ToolResult.failure(f"'{name}' was not found on this PC (not in PATH, App Paths or the Start menu).")
    except OSError as exc:
        return ToolResult.failure(f"'{name}' could not be started: {exc}")
    log.info("Start requested: %s via %s (%s)", name, kind, target)

    if alias in _ALWAYS_RUNNING:
        return ToolResult.success(
            f"Start of '{name}' was requested. This program always has a process, so success cannot be verified.",
            verified=False,
        )

    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    while True:
        now = _running(patterns)
        if now:
            if set(now) - before:
                return ToolResult.success(f"'{name}' was started and its process is running.", verified=True)
            return ToolResult.success(
                f"'{name}' is running (it was already running before; a new window may have opened).",
                verified=True,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    return ToolResult.success(
        f"The start command for '{name}' was sent, but no matching process was detected within "
        f"{VERIFY_TIMEOUT_S:.0f} seconds. The program may use a different process name or may still be starting.",
        verified=False,
    )


# --------------------------------------------------------------------------- #
# Programm schließen
# --------------------------------------------------------------------------- #
def _post_close_windows(pid: int) -> int:
    """Schickt WM_CLOSE an alle sichtbaren Fenster des Prozesses. Wird in Tests ersetzt."""
    import win32con
    import win32gui
    import win32process

    count = 0

    def _callback(hwnd, _):
        nonlocal count
        if win32gui.IsWindowVisible(hwnd):
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid == pid:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                count += 1
        return True

    win32gui.EnumWindows(_callback, None)
    return count


def _wait_gone(procs: dict[int, psutil.Process], timeout: float) -> list[psutil.Process]:
    _, alive = psutil.wait_procs(list(procs.values()), timeout=timeout)
    return alive


def close_application(app_name: str, force: bool = False) -> ToolResult:
    try:
        name = security.validate_app_name(app_name)
    except ValueError as exc:
        return ToolResult.failure(str(exc))

    key = _normalize(name)
    alias = APP_ALIASES.get(key, key)
    patterns = PROCESS_NAMES.get(alias, [alias.replace(" ", "")])

    if any(p in PROTECTED_PROCESSES for p in patterns):
        return ToolResult.failure(f"'{name}' is a protected process and is never closed by EVOX.")

    own = _own_pids()
    procs = {pid: p for pid, p in _running(patterns, exact=True).items() if pid not in own}
    if not procs:
        return ToolResult.failure(f"No running process named '{name}' was found.")

    windows_closed = 0
    for pid in procs:
        try:
            windows_closed += _post_close_windows(pid)
        except Exception as exc:  # noqa: BLE001 - win32 nicht verfügbar o. Ä.
            log.warning("WM_CLOSE for pid %s failed: %s", pid, exc)

    alive = _wait_gone(procs, CLOSE_TIMEOUT_S) if windows_closed else list(procs.values())

    if not alive:
        return ToolResult.success(f"'{name}' was closed ({len(procs)} process(es) ended).", verified=True)

    if not force:
        if windows_closed == 0:
            return ToolResult.failure(
                f"'{name}' has no window that could be closed gracefully. "
                "It can only be ended forcefully (force=true, needs user approval)."
            )
        return ToolResult.failure(
            f"'{name}' was asked to close but is still running. It may be waiting for the user "
            "(for example to save changes). Nothing was forced."
        )

    for p in alive:
        try:
            p.terminate()
        except psutil.Error:
            pass
    still = _wait_gone({p.pid: p for p in alive}, 3.0)
    for p in still:
        try:
            p.kill()
        except psutil.Error:
            pass
    still = _wait_gone({p.pid: p for p in still}, 3.0) if still else []

    if still:
        return ToolResult.failure(f"'{name}' could not be ended even by force ({len(still)} process(es) remain).")
    return ToolResult.success(f"'{name}' was ended forcefully; unsaved data may be lost.", verified=True)


# --------------------------------------------------------------------------- #
# VS Code
# --------------------------------------------------------------------------- #
def open_in_vscode(path: Path, reuse_window: bool = True) -> ToolResult:
    code = find_vscode()
    if code is None:
        return ToolResult.failure("Visual Studio Code was not found.")

    path_str = str(path)
    if code.suffix.lower() in (".cmd", ".bat") and any(ch in _CMD_METACHARS for ch in path_str):
        return ToolResult.failure("The path contains special characters that cannot be passed safely to code.cmd.")

    args = [str(code)] + (["-r"] if reuse_window else []) + [path_str]
    try:
        subprocess.Popen(
            args, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return ToolResult.failure(f"VS Code could not be started: {exc}")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _running(["code"], exact=True):
            return ToolResult.success("Opened in VS Code.", verified=True)
        time.sleep(0.25)
    return ToolResult.success("VS Code was started, but its process was not detected yet.", verified=False)
