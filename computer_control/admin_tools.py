"""
Administrator-Werkzeuge.

Wichtig zu den Grenzen (bewusst ehrlich gehalten):
  - ShellExecute("runas") startet ein Programm mit UAC-Abfrage. EVOX bekommt dabei
    weder Ausgabe noch Exit-Code. Das Ergebnis ist daher höchstens "unverified".
  - Für Revo Uninstaller sind in EVOX keine geprüften Kommandozeilenparameter
    hinterlegt. EVOX startet Revo nur und prüft, ob das Programm installiert ist.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from config import settings
from core.tool_result import ToolResult

log = logging.getLogger("evox.admin")

_SHELLEXECUTE_ERRORS = {
    0: "out of memory or resources",
    2: "file not found",
    3: "path not found",
    5: "access denied (the UAC prompt was probably declined)",
    8: "not enough memory",
    26: "sharing violation",
    27: "file association incomplete",
    31: "no application associated with this file type",
}

REVO_EXE_NAMES = ("RevoUnin.exe", "RevoUninPro.exe")


def _shell_execute_runas(exe: str, params: str) -> int:
    """Ruft ShellExecuteW mit 'runas' auf und gibt den Rückgabecode zurück (>32 = Erfolg)."""
    import ctypes

    fn = ctypes.windll.shell32.ShellExecuteW
    fn.restype = ctypes.c_void_p
    rc = fn(None, "runas", exe, params or None, None, 1)  # 1 = SW_SHOWNORMAL
    return int(rc or 0)


class AdminTools:
    # -- Befehle mit Administratorrechten ------------------------------------ #
    @staticmethod
    def parse_command(command: str) -> tuple[str, str]:
        """Zerlegt 'sfc /scannow' in ('sfc', '/scannow'). Wirft ValueError bei ungültiger Eingabe."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("The command is empty.")
        cmd = command.strip()
        if "\x00" in cmd:
            raise ValueError("The command contains an invalid character.")
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            if end == -1:
                raise ValueError("Unbalanced quotes in the command.")
            exe, rest = cmd[1:end], cmd[end + 1:].strip()
        else:
            parts = cmd.split(None, 1)
            exe, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        return exe, rest

    @staticmethod
    def check_command(command: str) -> str | None:
        """Gibt eine Fehlermeldung zurück, wenn der Befehl nicht erlaubt ist, sonst None."""
        try:
            exe, _ = AdminTools.parse_command(command)
        except ValueError as exc:
            return str(exc)
        if "\\" in exe or "/" in exe or ":" in exe:
            return "Use the bare program name (for example 'sfc'), not a path."
        stem = exe.lower()
        if stem.endswith(".exe"):
            stem = stem[:-4]
        if not settings.ADMIN_ALLOW_ANY_EXECUTABLE and stem not in settings.ADMIN_ALLOWED_EXECUTABLES:
            allowed = ", ".join(sorted(settings.ADMIN_ALLOWED_EXECUTABLES))
            return f"'{stem}' is not on the list of programs EVOX may start as administrator. Allowed: {allowed}."
        return None

    @staticmethod
    def execute_as_admin(command: str) -> ToolResult:
        error = AdminTools.check_command(command)
        if error:
            return ToolResult.failure(error)
        exe, params = AdminTools.parse_command(command)
        try:
            rc = _shell_execute_runas(exe, params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"The elevated start could not be requested: {exc}")
        log.info("Admin start requested: %s %s -> ShellExecute code %s", exe, params, rc)
        if rc <= 32:
            reason = _SHELLEXECUTE_ERRORS.get(rc, f"error code {rc}")
            return ToolResult.failure(f"Windows did not start '{exe}' as administrator: {reason}.")
        shown = (exe + " " + params).strip()
        return ToolResult.success(
            f"'{shown}' was started with administrator rights (UAC accepted). "
            "EVOX cannot read the program's output or exit code, so the outcome is unverified.",
            verified=False,
        )

    # -- Revo Uninstaller ------------------------------------------------------ #
    @staticmethod
    def _revo_dirs_from_registry() -> list[Path]:
        try:
            from system_administration.program_manager import ProgramManager

            dirs = []
            for entry in ProgramManager.find_program("Revo Uninstaller"):
                raw = entry.get("uninstall_string", "").strip()
                exe = raw.strip('"').split('" ')[0] if raw.startswith('"') else raw.split(" /")[0]
                parent = Path(exe.strip('"')).parent
                if str(parent) not in ("", "."):
                    dirs.append(parent)
            return dirs
        except Exception:  # noqa: BLE001 - nicht unter Windows / keine Registry
            return []

    @staticmethod
    def find_revo() -> Path | None:
        candidates: list[Path] = []
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                for sub in ("Revo Uninstaller", "Revo Uninstaller Pro"):
                    candidates.append(Path(base) / "VS Revo Group" / sub)
        candidates.extend(AdminTools._revo_dirs_from_registry())
        for directory in candidates:
            for exe_name in REVO_EXE_NAMES:
                exe = directory / exe_name
                if exe.exists():
                    return exe
        for exe_name in REVO_EXE_NAMES:
            found = shutil.which(exe_name)
            if found:
                return Path(found)
        return None

    @staticmethod
    def uninstall_with_revo(program_name: str) -> ToolResult:
        if not isinstance(program_name, str) or not program_name.strip() or len(program_name) > 100:
            return ToolResult.failure("The program name is empty or too long.")
        query = program_name.strip()

        try:
            from system_administration.program_manager import ProgramManager

            matches = ProgramManager.find_program(query)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"The list of installed programs could not be read: {exc}")

        if not matches:
            return ToolResult.failure(f"No installed program matching '{query}' was found.")
        exact = [m for m in matches if m["name"].lower() == query.lower()]
        if len(matches) > 1 and len(exact) != 1:
            names = ", ".join(m["name"] for m in matches[:8])
            return ToolResult.failure(f"'{query}' is ambiguous. Matches: {names}. Ask the user which one is meant.")
        target = (exact or matches)[0]["name"]

        revo = AdminTools.find_revo()
        if revo is None:
            return ToolResult.failure(
                "Revo Uninstaller was not found. Install it, or use the standard Windows uninstaller."
            )

        try:
            if not hasattr(os, "startfile"):
                raise OSError("Starting Revo is only supported on Windows.")
            os.startfile(str(revo))  # noqa: S606 - ShellExecute, löst bei Bedarf die UAC-Abfrage aus
        except OSError as exc:
            return ToolResult.failure(f"Revo Uninstaller could not be started: {exc}")

        log.info("Revo started for program '%s' (%s)", target, revo)
        return ToolResult.success(
            f"Revo Uninstaller was started. The program '{target}' is installed. "
            "EVOX has no verified command-line parameters for Revo, so the uninstall itself "
            f"must be done in Revo's window: select '{target}' there. "
            "EVOX cannot confirm that the uninstall happens or completes.",
            verified=False,
            program=target,
            revo=str(revo),
        )
