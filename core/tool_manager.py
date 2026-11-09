"""
Zentrale Tool-Registry für EVOX.

Jedes Werkzeug ist genau einmal als ToolSpec registriert. Die Tool-Definitionen für das
Sprachmodell werden daraus ERZEUGT – es kann also kein Tool mehr geben, das angeboten, aber
nicht implementiert ist (und umgekehrt).

Ablauf eines Aufrufs:
  1. prepare_call():  Argumente prüfen, Pfade/Namen prüfen, Risiko bestimmen
  2. (Aufrufer fragt bei hohem Risiko den Nutzer)
  3. execute_tool():  führt aus und liefert ein ToolResult mit ehrlichem Status
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

from config import settings
from core import security
from core.security import PathPolicy, RiskLevel
from core.tool_result import ToolResult

log = logging.getLogger("evox.tools")

MAX_SHORT_STRING = 4096
MAX_LIST_ENTRIES = 200


class Check(NamedTuple):
    error: str | None
    risk: RiskLevel
    detail: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict
    required: tuple
    handler: Callable[[dict], ToolResult]
    risk: RiskLevel = RiskLevel.LOW
    check: Callable[[dict], Check] | None = None
    allow_empty: tuple = ()      # Parameter, die leer sein dürfen
    long_params: tuple = ()      # Parameter mit großer Längengrenze (Dateiinhalt)


@dataclass(frozen=True)
class PreparedCall:
    name: str
    args: dict
    risk: RiskLevel
    needs_confirmation: bool
    description: str
    error: str | None = None


def _norm_newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


def _format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _folder_summary(path: Path, limit: int = 20000) -> tuple[int, int]:
    """(Anzahl Dateien, Gesamtgröße in Bytes), mit Obergrenze für riesige Ordner."""
    files = total = 0
    for dirpath, _dirs, filenames in os.walk(path):
        for fn in filenames:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
            if files >= limit:
                return files, total
    return files, total


class ToolManager:
    def __init__(self, policy: PathPolicy | None = None):
        self._policy = policy or security.default_policy()
        self._registry: dict[str, ToolSpec] = {}
        self._register_all()

    # ------------------------------------------------------------------ #
    # Registrierung
    # ------------------------------------------------------------------ #
    def _add(self, spec: ToolSpec) -> None:
        if spec.name in self._registry:
            raise ValueError(f"Tool registered twice: {spec.name}")
        self._registry[spec.name] = spec

    def _register_all(self) -> None:
        projects = settings.PROJECTS_DIR.as_posix()
        S = {"type": "string"}

        self._add(ToolSpec(
            "open_application",
            "Opens a program or game by its plain name (for example 'notepad', 'chrome', 'discord'). "
            "The result says whether the program was actually detected running.",
            {"app_name": {**S, "description": "Plain program name, no paths."}},
            ("app_name",), self._open_application, RiskLevel.LOW, self._check_open_application,
        ))
        self._add(ToolSpec(
            "close_application",
            "Closes a running program gracefully. Only set force=true if a graceful close failed; "
            "forcing needs user approval and can lose unsaved data.",
            {"app_name": {**S, "description": "Plain program name."},
             "force": {"type": "boolean", "description": "End the program forcefully."}},
            ("app_name",), self._close_application, RiskLevel.MEDIUM, self._check_close_application,
        ))
        self._add(ToolSpec(
            "create_web_project",
            f"Creates a new basic web project (index.html, style.css, script.js) in {projects} and opens it "
            "in VS Code. Fails if a project with that name already exists.",
            {"project_name": {**S, "description": "Folder name for the project."}},
            ("project_name",), self._create_web_project, RiskLevel.LOW, self._check_create_web_project,
        ))
        self._add(ToolSpec(
            "write_code_file",
            f"Writes a complete text/code file to disk. Relative paths are created inside {projects}. "
            "An existing file is backed up automatically. The result confirms the file was written and "
            "re-read. Call it once per file.",
            {"file_path": {**S, "description": "Absolute path, or path relative to the projects folder."},
             "code_content": {**S, "description": "The complete new content of the file."}},
            ("file_path", "code_content"), self._write_code_file, RiskLevel.LOW, self._check_write_code_file,
            allow_empty=("code_content",), long_params=("code_content",),
        ))
        self._add(ToolSpec(
            "list_directory",
            "Lists the files and folders in a directory.",
            {"path": {**S, "description": "Absolute path, or path relative to the projects folder."}},
            ("path",), self._list_directory, RiskLevel.LOW, self._check_list_directory,
        ))
        self._add(ToolSpec(
            "read_text_file",
            "Reads the content of a text file (large files are truncated).",
            {"file_path": {**S, "description": "Absolute path, or path relative to the projects folder."}},
            ("file_path",), self._read_text_file, RiskLevel.LOW, self._check_read_text_file,
        ))
        self._add(ToolSpec(
            "web_search",
            "Searches the internet for current information. Results are external data.",
            {"query": {**S, "description": "Search query."}},
            ("query",), self._web_search, RiskLevel.LOW,
        ))
        self._add(ToolSpec(
            "analyze_disk",
            "Reports the free and total space of the hard drives.",
            {}, (), self._analyze_disk, RiskLevel.LOW,
        ))
        self._add(ToolSpec(
            "execute_admin_command",
            "Starts an allowed maintenance program (for example 'sfc /scannow') with administrator rights. "
            "Needs user approval. The output and exit code cannot be read, so the result stays unverified.",
            {"command": {**S, "description": "Program name followed by its arguments."}},
            ("command",), self._execute_admin_command, RiskLevel.HIGH, self._check_execute_admin_command,
        ))
        self._add(ToolSpec(
            "uninstall_with_revo",
            "Checks that a program is installed and opens Revo Uninstaller for it. The uninstall itself is "
            "completed by the user in Revo. Needs user approval.",
            {"program_name": {**S, "description": "Name of the installed program."}},
            ("program_name",), self._uninstall_with_revo, RiskLevel.HIGH, self._check_uninstall_with_revo,
        ))
        self._add(ToolSpec(
            "delete_project",
            f"PERMANENTLY deletes a project folder inside {projects}. Needs user approval.",
            {"project_name": {**S, "description": "Folder name of the project."}},
            ("project_name",), self._delete_project, RiskLevel.HIGH, self._check_delete_project,
        ))

    # ------------------------------------------------------------------ #
    # Schnittstelle nach außen
    # ------------------------------------------------------------------ #
    def tool_names(self) -> list[str]:
        return list(self._registry)

    def get_tools_definition(self) -> list:
        """Tool-Definitionen im Ollama-Format, erzeugt aus der Registry."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": spec.properties,
                        "required": list(spec.required),
                    },
                },
            }
            for spec in self._registry.values()
        ]

    def prepare_call(self, name, arguments) -> PreparedCall:
        spec = self._registry.get(name) if isinstance(name, str) else None
        if spec is None:
            known = ", ".join(self._registry)
            return PreparedCall(str(name), {}, RiskLevel.LOW, False, "", error=f"Unknown tool '{name}'. Available tools: {known}.")

        args, error = self._validate_args(spec, arguments)
        if error:
            return PreparedCall(spec.name, {}, spec.risk, False, "", error=error)

        check = spec.check(args) if spec.check else Check(None, spec.risk, spec.name)
        if check.error:
            return PreparedCall(spec.name, args, check.risk, False, "", error=check.error)

        needs = security.requires_confirmation(check.risk)
        description = f"[{check.risk.name} RISK] {check.detail or spec.name}"
        return PreparedCall(spec.name, args, check.risk, needs, description)

    def execute_tool(self, tool_name: str, arguments, confirmed: bool = False) -> ToolResult:
        prepared = self.prepare_call(tool_name, arguments)
        if prepared.error:
            log.warning("Tool call rejected (%s): %s", tool_name, prepared.error)
            return ToolResult.failure(prepared.error)
        if prepared.needs_confirmation and not confirmed:
            log.warning("Tool call blocked, confirmation missing: %s", prepared.description)
            return ToolResult.failure("This action needs explicit user approval, which was not given.")

        spec = self._registry[tool_name]
        log.info("Tool start: %s", prepared.description)
        started = time.monotonic()
        try:
            result = spec.handler(prepared.args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Tool crashed: %s", tool_name)
            return ToolResult.failure(f"The tool '{tool_name}' crashed: {type(exc).__name__}: {exc}")
        if not isinstance(result, ToolResult):
            result = ToolResult.success(str(result))
        log.info("Tool end: %s -> %s (%.2fs)", tool_name, result.status, time.monotonic() - started)
        return result

    # ------------------------------------------------------------------ #
    # Argumentprüfung
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_args(spec: ToolSpec, raw) -> tuple[dict, str | None]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return {}, "The tool arguments must be a JSON object."

        clean: dict = {}
        for key, schema in spec.properties.items():
            if key not in raw or raw[key] is None:
                continue
            value = raw[key]
            expected = schema.get("type", "string")
            if expected == "string":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(value)
                if not isinstance(value, str):
                    return {}, f"Parameter '{key}' must be a string."
                if "\x00" in value:
                    return {}, f"Parameter '{key}' contains an invalid character."
                limit = security.MAX_WRITE_BYTES if key in spec.long_params else MAX_SHORT_STRING
                if len(value) > limit:
                    return {}, f"Parameter '{key}' is too long (maximum {limit} characters)."
            elif expected == "boolean":
                if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                    value = value.strip().lower() == "true"
                if not isinstance(value, bool):
                    return {}, f"Parameter '{key}' must be true or false."
            clean[key] = value

        for key in spec.required:
            if key not in clean:
                return {}, f"Required parameter '{key}' is missing."
            if key not in spec.allow_empty and isinstance(clean[key], str) and not clean[key].strip():
                return {}, f"Parameter '{key}' must not be empty."
        return clean, None

    # ------------------------------------------------------------------ #
    # Vorabprüfungen (Risiko + Gültigkeit), laufen VOR einer Rückfrage
    # ------------------------------------------------------------------ #
    def _check_open_application(self, args: dict) -> Check:
        try:
            name = security.validate_app_name(args["app_name"])
        except ValueError as exc:
            return Check(str(exc), RiskLevel.LOW, "")
        return Check(None, RiskLevel.LOW, f"Open application: {name}")

    def _check_close_application(self, args: dict) -> Check:
        try:
            name = security.validate_app_name(args["app_name"])
        except ValueError as exc:
            return Check(str(exc), RiskLevel.LOW, "")
        if args.get("force"):
            return Check(None, RiskLevel.HIGH, f"FORCE-CLOSE application '{name}' (unsaved data may be lost)")
        return Check(None, RiskLevel.MEDIUM, f"Close application: {name}")

    def _check_create_web_project(self, args: dict) -> Check:
        try:
            name = security.validate_project_name(args["project_name"])
        except ValueError as exc:
            return Check(str(exc), RiskLevel.LOW, "")
        if (settings.PROJECTS_DIR / name).exists():
            return Check(
                f"A project named '{name}' already exists in {settings.PROJECTS_DIR}. Choose another name.",
                RiskLevel.LOW, "",
            )
        return Check(None, RiskLevel.LOW, f"Create web project '{name}'")

    def _check_write_code_file(self, args: dict) -> Check:
        decision = self._policy.check_write(args["file_path"])
        if not decision.allowed:
            return Check(decision.reason, RiskLevel.LOW, "")
        size = len(args["code_content"].encode("utf-8"))
        if size > security.MAX_WRITE_BYTES:
            return Check(f"The content is too large ({size} bytes, maximum {security.MAX_WRITE_BYTES}).", RiskLevel.LOW, "")
        action = "Overwrite file" if decision.path.exists() else "Write file"
        detail = f"{action} {decision.path} ({_format_size(size)})"
        if decision.needs_confirmation:
            return Check(None, RiskLevel.HIGH, f"{detail} - approval needed because: {decision.reason}")
        return Check(None, RiskLevel.LOW, detail)

    def _check_list_directory(self, args: dict) -> Check:
        decision = self._policy.check_read(args["path"])
        if not decision.allowed:
            return Check(decision.reason, RiskLevel.LOW, "")
        return Check(None, RiskLevel.LOW, f"List folder {decision.path}")

    def _check_read_text_file(self, args: dict) -> Check:
        decision = self._policy.check_read(args["file_path"])
        if not decision.allowed:
            return Check(decision.reason, RiskLevel.LOW, "")
        return Check(None, RiskLevel.LOW, f"Read file {decision.path}")

    def _check_execute_admin_command(self, args: dict) -> Check:
        from computer_control.admin_tools import AdminTools

        error = AdminTools.check_command(args["command"])
        if error:
            return Check(error, RiskLevel.HIGH, "")
        return Check(None, RiskLevel.HIGH, f"Run as ADMINISTRATOR: {args['command'].strip()}")

    def _check_uninstall_with_revo(self, args: dict) -> Check:
        name = args["program_name"].strip()
        if len(name) > 100:
            return Check("The program name is too long.", RiskLevel.HIGH, "")
        return Check(None, RiskLevel.HIGH, f"Open Revo Uninstaller to uninstall '{name}'")

    def _check_delete_project(self, args: dict) -> Check:
        try:
            name = security.validate_project_name(args["project_name"])
        except ValueError as exc:
            return Check(str(exc), RiskLevel.HIGH, "")
        path = settings.PROJECTS_DIR / name
        if not path.is_dir():
            return Check(f"Project '{name}' was not found in {settings.PROJECTS_DIR}.", RiskLevel.HIGH, "")
        files, size = _folder_summary(path)
        return Check(None, RiskLevel.HIGH, f"PERMANENTLY DELETE the project folder {path} ({files} files, {_format_size(size)})")

    # ------------------------------------------------------------------ #
    # Handler
    # ------------------------------------------------------------------ #
    def _open_application(self, args: dict) -> ToolResult:
        from computer_control import app_launcher

        return app_launcher.open_application(args["app_name"])

    def _close_application(self, args: dict) -> ToolResult:
        from computer_control import app_launcher

        return app_launcher.close_application(args["app_name"], force=bool(args.get("force", False)))

    def _create_web_project(self, args: dict) -> ToolResult:
        from system_administration.project_generator import ProjectGenerator

        return ProjectGenerator.create_web_project(args["project_name"])

    def _backup_file(self, path: Path) -> Path:
        """Sichert eine bestehende Datei vor dem Überschreiben. Wirft OSError bei Misserfolg."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = settings.BACKUP_DIR / stamp
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{path.parent.name}__{path.name}"
        counter = 1
        while target.exists():
            target = folder / f"{path.parent.name}__{path.name}.{counter}"
            counter += 1
        target.write_bytes(path.read_bytes())
        return target

    def _write_code_file(self, args: dict) -> ToolResult:
        decision = self._policy.check_write(args["file_path"])
        if not decision.allowed:
            return ToolResult.failure(decision.reason)
        path: Path = decision.path
        content: str = args["code_content"]
        data = content.encode("utf-8")
        if len(data) > security.MAX_WRITE_BYTES:
            return ToolResult.failure("The content is too large.")
        if path.exists() and not path.is_file():
            return ToolResult.failure(f"'{path}' exists and is not a regular file.")

        backup_note = ""
        tmp = path.with_name(path.name + ".evox_tmp")
        try:
            if path.is_file():
                if path.read_bytes() != data:
                    backup = self._backup_file(path)
                    backup_note = f" Previous version saved to {backup}."
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult.failure(f"The file '{path}' could not be written: {exc}")

        # Ergebnis prüfen: Datei zurücklesen und vergleichen
        try:
            written = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.failure(f"The file '{path}' was written but could not be read back: {exc}")
        if _norm_newlines(written) != _norm_newlines(content):
            return ToolResult.failure(f"Verification failed: the content of '{path}' differs from what was written.")

        note = ""
        if settings.VSCODE_OPEN_ON_WRITE:
            note = self._open_in_vscode(path)
        return ToolResult.success(
            f"Wrote {path} ({_format_size(len(data))}) and verified it by reading it back.{backup_note}{note}",
            verified=True,
            path=str(path),
        )

    def _open_in_vscode(self, path: Path) -> str:
        try:
            from computer_control import app_launcher

            vs = app_launcher.open_in_vscode(path, reuse_window=True)
            if not vs.ok:
                return f" (VS Code could not be opened: {vs.message})"
            if settings.VSCODE_AUTO_CENTER:
                try:
                    from computer_control.ui_automation import UIAutomation

                    time.sleep(1.0)
                    UIAutomation.center_window("Visual Studio Code")
                    UIAutomation.focus_window("Visual Studio Code")
                except Exception as exc:  # noqa: BLE001
                    log.debug("VS Code window could not be centered: %s", exc)
            return " Opened in VS Code."
        except Exception as exc:  # noqa: BLE001
            return f" (VS Code could not be opened: {exc})"

    def _list_directory(self, args: dict) -> ToolResult:
        decision = self._policy.check_read(args["path"])
        if not decision.allowed:
            return ToolResult.failure(decision.reason)
        path: Path = decision.path
        if not path.is_dir():
            return ToolResult.failure(f"'{path}' is not an existing folder.")

        from system_administration.file_manager import FileManager

        items = FileManager.list_directory(str(path))
        if not items:
            return ToolResult.success(f"The folder {path} is empty (or could not be listed).", verified=True)
        lines = [f"{'[DIR]' if i['is_dir'] else '[FILE]'} {i['path']}" for i in items[:MAX_LIST_ENTRIES]]
        if len(items) > MAX_LIST_ENTRIES:
            lines.append(f"... and {len(items) - MAX_LIST_ENTRIES} more entries")
        return ToolResult.success(security.wrap_untrusted("directory_listing", "\n".join(lines)), verified=True)

    def _read_text_file(self, args: dict) -> ToolResult:
        decision = self._policy.check_read(args["file_path"])
        if not decision.allowed:
            return ToolResult.failure(decision.reason)
        path: Path = decision.path
        if not path.is_file():
            return ToolResult.failure(f"'{path}' is not an existing file.")
        try:
            with open(path, "rb") as f:
                raw = f.read(security.MAX_READ_BYTES + 1)
        except OSError as exc:
            return ToolResult.failure(f"The file '{path}' could not be read: {exc}")
        if b"\x00" in raw[:4096]:
            return ToolResult.failure(f"'{path}' looks like a binary file and is not read.")
        truncated = len(raw) > security.MAX_READ_BYTES
        text = raw[: security.MAX_READ_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n[... truncated after {security.MAX_READ_BYTES} bytes ...]"
        return ToolResult.success(
            security.wrap_untrusted("file", text, max_chars=security.MAX_READ_BYTES + 200), verified=True
        )

    def _web_search(self, args: dict) -> ToolResult:
        from internet.web_search import WebSearch

        return WebSearch.search(args["query"])

    def _analyze_disk(self, args: dict) -> ToolResult:
        from system_administration.disk_analyzer import DiskAnalyzer

        drives = DiskAnalyzer.get_drive_info()
        if not drives:
            return ToolResult.failure("No drive information could be read.")
        text = "Drive analysis:\n" + "\n".join(
            f"- {d['drive']}: {d['free_gb']} GB free out of {d['total_gb']} GB" for d in drives
        )
        return ToolResult.success(text, verified=True)

    def _execute_admin_command(self, args: dict) -> ToolResult:
        from computer_control.admin_tools import AdminTools

        return AdminTools.execute_as_admin(args["command"])

    def _uninstall_with_revo(self, args: dict) -> ToolResult:
        from computer_control.admin_tools import AdminTools

        return AdminTools.uninstall_with_revo(args["program_name"])

    def _delete_project(self, args: dict) -> ToolResult:
        from system_administration.file_manager import FileManager

        return FileManager.delete_project(args["project_name"])
