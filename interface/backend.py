import logging
import threading
from datetime import datetime
import httpx
import psutil
import pynvml
from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from config import settings
from core import ai_engine
from core.context_manager import ContextManager
from core.ollama_utils import model_is_installed
from core.tool_manager import ToolManager
from memory.database import Database
from memory.memory_manager import MemoryManager
from memory.extractor import MemoryExtractor
from voice.text_to_speech import TextToSpeech
from voice.speech_to_text import VoiceAssistant
from system_administration.file_manager import FileManager

log = logging.getLogger("evox.backend")

OLLAMA_RECHECK_MS = 30_000


class Backend(QObject):
    # --- Streaming ---
    responseChunk = Signal(str)
    responseFinished = Signal(str)
    responseError = Signal(str)

    # --- Historie ---
    historyChanged = Signal()
    historyReloaded = Signal()

    # --- Status ---
    stateChanged = Signal()
    ollamaStatusChanged = Signal()
    systemStatsChanged = Signal()
    timeChanged = Signal()

    # --- Gedächtnis ---
    memoryChanged = Signal()
    memoryAdded = Signal(str)

    # --- Sicherheit & System ---
    confirmRequested = Signal(str)
    confirmCancelled = Signal()          # NEU: Zustimmung abgelaufen -> Popup schließen
    systemMessageAdded = Signal(str)

    # --- Sprache ---
    textRecognized = Signal(str)

    # --- intern: Ergebnis der Ollama-Prüfung aus dem Hintergrund-Thread ---
    ollamaProbed = Signal(bool, str)

    def __init__(self):
        super().__init__()

        self._db = Database()
        self._mm = MemoryManager(self._db)
        self._cm = ContextManager(self._mm)
        self._tm = ToolManager()
        self._tts = TextToSpeech()

        # Voice Assistant
        self._voice = VoiceAssistant()
        self._voice.transcriptionReady.connect(self.textRecognized.emit)
        self._voice.listeningChanged.connect(self._on_listening_changed)
        self._is_listening = False

        self._conversation_id = self._ensure_active_conversation()
        self._history = self._load_history()

        self._state = "idle"
        self._ollama_ok = False
        self._ollama_message = "Nicht verbunden"
        self._ollama_probe_running = False
        self._cpu = 0.0
        self._ram = 0.0
        self._gpu = 0.0
        self._thread = None
        self._worker = None
        self._extractor_thread = None

        # GPU Initialisierung
        try:
            pynvml.nvmlInit()
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._has_gpu = True
        except Exception:
            self._has_gpu = False
            self._nvml_handle = None

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._tick)
        self._stats_timer.start(1000)

        # Ollama-Status: Ergebnis kommt aus einem Hintergrund-Thread, die Oberfläche blockiert nicht
        self.ollamaProbed.connect(self._apply_ollama_status)
        self._ollama_timer = QTimer(self)
        self._ollama_timer.timeout.connect(self._check_ollama)
        self._ollama_timer.start(OLLAMA_RECHECK_MS)
        self._check_ollama()

    def _ensure_active_conversation(self) -> int:
        existing = self._db.get_setting("current_conversation_id")
        if existing:
            row = self._db.query_one("SELECT id FROM conversations WHERE id = ?", (int(existing),))
            if row:
                return int(existing)
        new_id = self._db.insert_returning_id("INSERT INTO conversations DEFAULT VALUES")
        self._db.set_setting("current_conversation_id", str(new_id))
        return new_id

    def _load_history(self) -> list[dict]:
        rows = self._db.query(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "AND role IN ('user', 'assistant') ORDER BY id ASC",
            (self._conversation_id,),
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def _tick(self) -> None:
        try:
            self._cpu = psutil.cpu_percent(interval=None)
            self._ram = psutil.virtual_memory().percent

            if self._has_gpu:
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                    self._gpu = float(util.gpu)
                except Exception:
                    self._gpu = 0.0
        except Exception:
            pass
        self.systemStatsChanged.emit()
        self.timeChanged.emit()

    def _set_state(self, new_state: str) -> None:
        if new_state != self._state:
            self._state = new_state
            self.stateChanged.emit()

    def _set_ollama(self, ok: bool, message: str) -> None:
        if ok != self._ollama_ok or message != self._ollama_message:
            self._ollama_ok = ok
            self._ollama_message = message
            self.ollamaStatusChanged.emit()

    @Slot(bool, str)
    def _apply_ollama_status(self, ok: bool, message: str) -> None:
        self._set_ollama(ok, message)

    def _check_ollama(self) -> None:
        """Startet die Prüfung im Hintergrund (blockiert die Oberfläche nicht)."""
        if self._ollama_probe_running:
            return
        self._ollama_probe_running = True
        threading.Thread(target=self._probe_ollama, daemon=True).start()

    def _probe_ollama(self) -> None:
        ok, message = False, ""
        try:
            r = httpx.get(settings.OLLAMA_TAGS_ENDPOINT, timeout=3.0)
            if r.status_code == 200:
                names = [m.get("name", "") for m in r.json().get("models", [])]
                if model_is_installed(settings.OLLAMA_MODEL, names):
                    ok, message = True, f"Verbunden · {settings.OLLAMA_MODEL} bereit"
                else:
                    message = f"Modell {settings.OLLAMA_MODEL} fehlt (ollama pull {settings.OLLAMA_MODEL})"
            else:
                message = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            message = f"Nicht erreichbar ({type(exc).__name__})"
        self._ollama_probe_running = False
        self.ollamaProbed.emit(ok, message)

    @Slot()
    def refreshOllamaStatus(self) -> None:
        self._check_ollama()

    @Slot(str)
    def sendMessage(self, text: str) -> None:
        text = text.strip()
        if not text or self._thread is not None:
            return

        self._db.insert_returning_id(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (self._conversation_id, text),
        )
        self._history.append({"role": "user", "content": text})
        self.historyChanged.emit()
        self._set_state("thinking")

        messages = self._cm.build_messages(self._history)

        worker = ai_engine.ChatWorker(
            messages, settings.OLLAMA_MODEL,
            tools=self._tm.get_tools_definition(),
            tool_manager=self._tm,
        )

        # WICHTIG: Worker ZUERST zuweisen, damit respondToConfirmation darauf zugreifen kann
        self._worker = worker

        worker.chunk.connect(self.responseChunk)
        worker.finished.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        worker.confirmRequired.connect(self._on_confirm_required)
        worker.confirmCancelled.connect(self.confirmCancelled)
        worker.systemMessage.connect(self.systemMessageAdded.emit)

        thread = threading.Thread(target=worker.run, daemon=True)
        self._thread = thread
        thread.start()

    @Slot()
    def startListening(self):
        self._voice.start_recording()

    @Slot()
    def stopListening(self):
        self._voice.stop_recording()

    def _on_listening_changed(self, is_listening: bool):
        self._is_listening = is_listening
        self.systemStatsChanged.emit()

    @Slot(bool)
    def respondToConfirmation(self, confirmed: bool) -> None:
        if self._worker:
            self._worker.set_confirmation(confirmed)

    @Slot(str, str)
    def _on_confirm_required(self, tool_name: str, description: str) -> None:
        # Sendet die Beschreibung (Werkzeug, Risiko, Details) an das QML-Popup
        self.confirmRequested.emit(description)

    @Slot()
    def clearHistory(self) -> None:
        new_id = self._db.insert_returning_id("INSERT INTO conversations DEFAULT VALUES")
        self._db.set_setting("current_conversation_id", str(new_id))
        self._conversation_id = new_id
        self._history = []
        self.historyChanged.emit()
        self.historyReloaded.emit()

    @Slot(result="QVariantList")
    def getCurrentMessages(self) -> list:
        rows = self._db.query(
            "SELECT role, content, created_at FROM messages "
            "WHERE conversation_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY id ASC",
            (self._conversation_id,),
        )
        out = []
        for r in rows:
            created = str(r["created_at"])
            time_part = created[11:16] if len(created) >= 16 else ""
            out.append({"role": r["role"], "content": r["content"], "time": time_part})
        return out

    @Slot(str)
    def _on_worker_finished(self, full: str) -> None:
        self._history.append({"role": "assistant", "content": full})
        self._db.insert_returning_id(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
            (self._conversation_id, full),
        )
        self.historyChanged.emit()
        self._set_state("idle")
        self.responseFinished.emit(full)
        self._thread = None
        self._worker = None

        if full.strip():
            self._tts.speak(full)

        self._maybe_extract_memories()

    @Slot(str)
    def _on_worker_failed(self, error: str) -> None:
        log.error("Worker failed: %s", error)
        self._set_state("error")
        self.responseError.emit(error)
        self._thread = None
        self._worker = None
        QTimer.singleShot(4000, lambda: self._set_state("idle"))

    def _maybe_extract_memories(self) -> None:
        if not settings.MEMORY_EXTRACTION_ENABLED:
            return
        if len(self._history) < 2:
            return
        last_user = self._history[-2]
        last_assistant = self._history[-1]
        if last_user["role"] != "user" or last_assistant["role"] != "assistant":
            return
        if len(last_user["content"]) < settings.MEMORY_MIN_USER_LEN:
            return

        exchange = f"Nutzer: {last_user['content']}\nAssistent: {last_assistant['content'][:800]}"
        known = self._mm.all_contents()

        extractor = MemoryExtractor(exchange, known, settings.OLLAMA_MODEL)
        extractor.extracted.connect(self._on_memories_extracted)
        extractor.failed.connect(lambda e: log.warning("Memory extraction failed: %s", e))

        t = threading.Thread(target=extractor.run, daemon=True)
        self._extractor_thread = t
        t.start()

    @Slot(list)
    def _on_memories_extracted(self, items: list) -> None:
        added = False
        for item in items:
            result = self._mm.add(item["category"], item["content"], source="auto")
            if result:
                added = True
                self.memoryAdded.emit(item["content"])
        if added:
            self.memoryChanged.emit()

    @Slot(int, str)
    def updateMemory(self, mem_id: int, content: str) -> None:
        if self._mm.update(mem_id, content):
            self.memoryChanged.emit()

    @Slot(int)
    def deleteMemory(self, mem_id: int) -> None:
        if self._mm.delete(mem_id):
            self.memoryChanged.emit()

    @Slot(result=str)
    def getHomeDirectory(self) -> str:
        import os
        return os.path.expanduser("~").replace("\\", "/")

    @Slot(str, result="QVariantList")
    def listDirectory(self, path: str) -> list:
        return FileManager.list_directory(path)

    @Property(str, notify=stateChanged)
    def state(self): return self._state

    @Property(bool, notify=ollamaStatusChanged)
    def ollamaOk(self): return self._ollama_ok

    @Property(str, notify=ollamaStatusChanged)
    def ollamaMessage(self): return self._ollama_message

    @Property(float, notify=systemStatsChanged)
    def cpuPercent(self): return self._cpu

    @Property(float, notify=systemStatsChanged)
    def ramPercent(self): return self._ram

    @Property(float, notify=systemStatsChanged)
    def gpuPercent(self): return self._gpu

    @Property(bool, notify=systemStatsChanged)
    def isListening(self): return self._is_listening

    @Property(str, notify=timeChanged)
    def currentTime(self): return datetime.now().strftime("%H:%M:%S")

    @Property(str, notify=timeChanged)
    def currentDate(self): return datetime.now().strftime("%A · %d.%m.%Y")

    @Property(str, constant=True)
    def modelName(self): return settings.OLLAMA_MODEL

    @Property("QVariantList", notify=memoryChanged)
    def memories(self): return self._mm.list_all()

    @Property(int, notify=memoryChanged)
    def memoryCount(self): return len(self._mm.list_all())
