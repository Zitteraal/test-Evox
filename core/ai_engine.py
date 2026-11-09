import json
import logging
import threading

import httpx
from PySide6.QtCore import QObject, Signal

from config import settings
from core.tool_result import ToolResult

log = logging.getLogger("evox.ai")

# Texte für Rückfälle (nur wenn das Modell selbst keine Antwort liefert)
_TEXTS = {
    "de": {
        "empty": "Das Modell hat keine Antwort geliefert.",
        "summary": "{ok} von {total} Aktion(en) erfolgreich ({verified} davon geprüft), {failed} fehlgeschlagen.",
        "unverified": "{n} Aktion(en) ließen sich nicht bestätigen.",
        "failed_item": "Fehlgeschlagen: {name}: {message}",
        "exhausted": "Ich habe nach {rounds} Tool-Runden abgebrochen, ohne zu einem Ergebnis zu kommen.",
        "cancelled": "Von dir abgelehnt oder nicht rechtzeitig bestätigt.",
    },
    "en": {
        "empty": "The model returned no answer.",
        "summary": "{ok} of {total} action(s) succeeded ({verified} verified), {failed} failed.",
        "unverified": "{n} action(s) could not be confirmed.",
        "failed_item": "Failed: {name}: {message}",
        "exhausted": "I stopped after {rounds} tool rounds without reaching a result.",
        "cancelled": "Declined by you or not confirmed in time.",
    },
}


def _texts() -> dict:
    # "auto" und "de" -> Deutsch (die Oberfläche ist deutschsprachig), "en" -> Englisch
    return _TEXTS["en"] if settings.RESPONSE_LANGUAGE == "en" else _TEXTS["de"]


class ChatWorker(QObject):
    chunk = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    confirmRequired = Signal(str, str)
    confirmCancelled = Signal()
    systemMessage = Signal(str)

    def __init__(self, messages: list[dict], model: str, tools: list | None = None, tool_manager=None):
        super().__init__()
        self._messages = messages
        self._model = model
        self._tools = tools or []
        self._tm = tool_manager
        self._confirm_event = threading.Event()
        self._is_confirmed = False
        self._results: list[tuple[str, ToolResult]] = []

    # ------------------------------------------------------------------ #
    # Zustimmung
    # ------------------------------------------------------------------ #
    def set_confirmation(self, confirmed: bool):
        self._is_confirmed = confirmed
        self._confirm_event.set()

    def _ask_confirmation(self, tool_name: str, description: str) -> bool:
        self._confirm_event.clear()
        self._is_confirmed = False
        self.confirmRequired.emit(tool_name, description)
        answered = self._confirm_event.wait(timeout=settings.CONFIRM_TIMEOUT_SECONDS)
        if not answered:
            log.warning("Confirmation timed out for %s", tool_name)
            self.confirmCancelled.emit()  # schließt das Popup in der Oberfläche
            return False
        return self._is_confirmed

    # ------------------------------------------------------------------ #
    # Hauptschleife
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        final_text = ""
        finished_naturally = False
        streamed_final = False
        try:
            tm = self._tm
            if tm is None:
                from core.tool_manager import ToolManager
                tm = ToolManager()

            with httpx.Client(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
                for _round in range(settings.MAX_TOOL_ROUNDS):
                    outcome = self._chat_round(client)
                    if outcome is None:
                        return  # Fehler wurde bereits über 'failed' gemeldet
                    message, streamed = outcome

                    tool_calls = message.get("tool_calls") or []
                    if not tool_calls:
                        final_text = message.get("content") or ""
                        streamed_final = streamed
                        finished_naturally = True
                        break

                    self._messages.append(message)
                    for tool_call in tool_calls:
                        self._handle_tool_call(tm, tool_call)

            if not final_text.strip():
                final_text = self._fallback_text(exhausted=not finished_naturally)
                streamed_final = False

            if not streamed_final:
                self.chunk.emit(final_text)
            self.finished.emit(final_text)

        except Exception as exc:  # noqa: BLE001
            log.exception("Chat worker failed")
            self.failed.emit(f"Fehler: {exc}")

    # ------------------------------------------------------------------ #
    # Eine Runde Modellaufruf
    # ------------------------------------------------------------------ #
    def _payload(self, stream: bool) -> dict:
        payload = {"model": self._model, "messages": self._messages, "stream": stream}
        if self._tools:
            payload["tools"] = self._tools
        return payload

    @staticmethod
    def _error_detail(resp) -> str:
        try:
            detail = resp.json().get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        return f" - {detail}" if detail else ""

    def _chat_round(self, client):
        """Gibt (message, wurde_gestreamt) zurück oder None, wenn ein Fehler gemeldet wurde."""
        if settings.STREAM_RESPONSES:
            return self._chat_round_stream(client)

        resp = client.post(settings.OLLAMA_CHAT_ENDPOINT, json=self._payload(stream=False))
        if resp.status_code != 200:
            self.failed.emit(f"Ollama Fehler: HTTP {resp.status_code}{self._error_detail(resp)}")
            return None
        return resp.json().get("message", {}) or {}, False

    def _chat_round_stream(self, client):
        content_parts: list[str] = []
        tool_calls: list = []
        with client.stream("POST", settings.OLLAMA_CHAT_ENDPOINT, json=self._payload(stream=True)) as resp:
            if resp.status_code != 200:
                resp.read()
                self.failed.emit(f"Ollama Fehler: HTTP {resp.status_code}{self._error_detail(resp)}")
                return None
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    self.failed.emit(f"Ollama Fehler: {obj['error']}")
                    return None
                msg = obj.get("message") or {}
                piece = msg.get("content") or ""
                if piece:
                    content_parts.append(piece)
                    self.chunk.emit(piece)
                tool_calls.extend(msg.get("tool_calls") or [])
                if obj.get("done"):
                    break
        message = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message, True

    # ------------------------------------------------------------------ #
    # Tool-Aufrufe
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_arguments(raw):
        """Gibt (dict, Fehlertext|None) zurück."""
        if raw is None or raw == "":
            return {}, None
        if isinstance(raw, dict):
            return raw, None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                log.warning("Tool arguments are not valid JSON: %s", exc)
                return {}, f"The tool arguments are not valid JSON ({exc.msg}). Send them again as a JSON object."
            if isinstance(parsed, dict):
                return parsed, None
        return {}, "The tool arguments must be a JSON object."

    def _record(self, tool_name: str, result: ToolResult) -> None:
        self._results.append((tool_name, result))
        self._messages.append({"role": "tool", "tool_name": tool_name, "content": result.to_model_text()})
        self.systemMessage.emit(result.to_display_text())

    def _handle_tool_call(self, tm, tool_call: dict) -> None:
        function = tool_call.get("function") or {}
        tool_name = function.get("name") or "unknown"
        arguments, parse_error = self._parse_arguments(function.get("arguments"))

        if parse_error:
            self._record(tool_name, ToolResult.failure(parse_error))
            return

        prepared = tm.prepare_call(tool_name, arguments)
        if prepared.error:
            self._record(tool_name, ToolResult.failure(prepared.error))
            return

        confirmed = False
        if prepared.needs_confirmation:
            if not self._ask_confirmation(tool_name, prepared.description):
                self._record(tool_name, ToolResult.failure(f"Action cancelled: {_texts()['cancelled']}"))
                return
            confirmed = True

        result = tm.execute_tool(tool_name, prepared.args, confirmed=confirmed)
        self._record(tool_name, result)

    # ------------------------------------------------------------------ #
    # Wahrheitsgemäßer Rückfalltext (nur wenn das Modell nichts geliefert hat)
    # ------------------------------------------------------------------ #
    def _fallback_text(self, exhausted: bool) -> str:
        t = _texts()
        if not self._results:
            return t["exhausted"].format(rounds=settings.MAX_TOOL_ROUNDS) if exhausted else t["empty"]

        total = len(self._results)
        ok = sum(1 for _, r in self._results if r.ok)
        verified = sum(1 for _, r in self._results if r.status == "ok")
        unverified = sum(1 for _, r in self._results if r.status == "unverified")
        failed = total - ok

        parts = []
        if exhausted:
            parts.append(t["exhausted"].format(rounds=settings.MAX_TOOL_ROUNDS))
        parts.append(t["summary"].format(ok=ok, total=total, verified=verified, failed=failed))
        if unverified:
            parts.append(t["unverified"].format(n=unverified))
        for name, result in [(n, r) for n, r in self._results if not r.ok][:3]:
            first_line = result.message.strip().splitlines()[0] if result.message.strip() else ""
            parts.append(t["failed_item"].format(name=name, message=first_line))
        return "\n".join(parts)
