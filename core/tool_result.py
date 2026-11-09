"""
Einheitliches Ergebnis-Objekt für alle EVOX-Tools.

Kernidee: Ein Tool darf nur dann "Erfolg" melden, wenn das Ergebnis geprüft wurde.
  - failed      -> die Aktion ist fehlgeschlagen
  - unverified  -> die Aktion wurde ausgelöst, der Erfolg ließ sich aber nicht bestätigen
  - ok          -> die Aktion wurde ausgeführt UND das Ergebnis wurde geprüft
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    verified: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, message: str, *, verified: bool = False, **data: Any) -> "ToolResult":
        return cls(ok=True, message=message, verified=verified, data=data)

    @classmethod
    def failure(cls, message: str, **data: Any) -> "ToolResult":
        return cls(ok=False, message=message, verified=False, data=data)

    @property
    def status(self) -> str:
        if not self.ok:
            return "failed"
        return "ok" if self.verified else "unverified"

    def to_model_text(self) -> str:
        """Text, den das Sprachmodell als Tool-Antwort bekommt."""
        label = {
            "ok": "SUCCESS (verified)",
            "unverified": "UNVERIFIED (the action was sent, but the result could not be confirmed)",
            "failed": "FAILED",
        }[self.status]
        return f"{label}: {self.message}"

    def to_display_text(self) -> str:
        """Kurzer Text für die Chat-Anzeige."""
        icon = {"ok": "✔", "unverified": "?", "failed": "✘"}[self.status]
        first_line = self.message.strip().splitlines()[0] if self.message.strip() else ""
        if len(first_line) > 300:
            first_line = first_line[:297] + "..."
        return f"{icon} {first_line}"
