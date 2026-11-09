import logging
import os
import shutil
from pathlib import Path

from config import settings
from core import security
from core.tool_result import ToolResult

log = logging.getLogger("evox.files")


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(is_junction and is_junction(path))


class FileManager:
    @staticmethod
    def create_text_file(file_path: str, content: str) -> str:
        try:
            if "desktop" in file_path.lower() or "schreibtisch" in file_path.lower():
                import win32com.client  # nur hier nötig, daher verzögert importiert

                shell = win32com.client.Dispatch("WScript.Shell")
                desktop_path = shell.SpecialFolders("Desktop")
                filename = os.path.basename(file_path)
                file_path = os.path.join(desktop_path, filename)
            else:
                file_path = os.path.expanduser(file_path)

            directory = os.path.dirname(file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return f"Success: File created successfully at {file_path}"
        except Exception as e:
            return f"Error creating file: {str(e)}"

    @staticmethod
    def list_directory(path: str) -> list:
        if not path or path == "~":
            path = os.path.expanduser("~")

        items = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir()
                        size = entry.stat().st_size if not is_dir else 0
                        items.append({
                            "name": entry.name,
                            "path": entry.path.replace("\\", "/"),
                            "is_dir": is_dir,
                            "size": size
                        })
                    except Exception:
                        pass
        except Exception:
            return []

        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items

    @staticmethod
    def delete_project(project_name: str) -> ToolResult:
        """
        Löscht einen Projektordner dauerhaft – aber nur direkt unterhalb des EVOX-Projektordners.
        Der Name wird geprüft (kein '..', keine Pfadtrenner), Links/Junctions werden nicht angefasst
        und das Ergebnis wird überprüft.
        """
        try:
            name = security.validate_project_name(project_name)
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        root = settings.PROJECTS_DIR.resolve()
        target = root / name
        if target.resolve().parent != root:
            return ToolResult.failure("The project path is not directly inside the EVOX projects folder.")
        if not target.exists():
            return ToolResult.failure(f"Project '{name}' was not found in {root}.")
        if _is_link_or_junction(target):
            return ToolResult.failure(f"'{name}' is a link/junction and is not deleted for safety.")
        if not target.is_dir():
            return ToolResult.failure(f"'{name}' is not a folder.")

        try:
            shutil.rmtree(target)
        except OSError as exc:
            still_there = target.exists()
            return ToolResult.failure(
                f"Project '{name}' could not be fully deleted: {exc}."
                + (" Some files may remain." if still_there else "")
            )

        if target.exists():
            return ToolResult.failure(f"Project '{name}' still exists after deleting.")
        log.warning("Project deleted: %s", target)
        return ToolResult.success(f"Project '{name}' was permanently deleted ({target}).", verified=True)
