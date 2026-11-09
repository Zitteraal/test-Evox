"""Kleine, abhängigkeitsfreie Hilfen für die Ollama-Anbindung."""
from __future__ import annotations

from typing import Iterable


def normalize_model_name(name: str) -> str:
    """'qwen3' und 'qwen3:latest' bezeichnen bei Ollama dasselbe Modell."""
    n = (name or "").strip().lower()
    return n if ":" in n else f"{n}:latest"


def model_is_installed(wanted: str, installed_names: Iterable[str]) -> bool:
    target = normalize_model_name(wanted)
    return any(normalize_model_name(n) == target for n in installed_names if n)
