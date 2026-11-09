import logging
import os
import re
import subprocess
import threading
import unicodedata

from config import settings

log = logging.getLogger("evox.tts")

ASSETS_DIR = settings.BASE_DIR / "assets"

# Piper-Stimmen je Sprache. Die deutsche Stimme muss separat heruntergeladen werden
# (z. B. de_DE-thorsten-high.onnx + .onnx.json in den Ordner "assets").
VOICE_FILES = {
    "en": "en_US-lessac-high.onnx",
    "de": "de_DE-thorsten-high.onnx",
}
MODEL_PATH = ASSETS_DIR / VOICE_FILES["en"]  # Rückwärtskompatibel

_GERMAN_WORDS = {
    "der", "die", "das", "und", "ist", "nicht", "ich", "du", "sie", "wir", "ein", "eine",
    "mit", "für", "auf", "den", "dem", "zu", "von", "auch", "es", "wurde", "habe", "hast",
    "kann", "bitte", "datei", "projekt", "fehler", "erfolgreich",
}
_ENGLISH_WORDS = {
    "the", "and", "is", "not", "you", "we", "a", "an", "with", "for", "on", "to", "of",
    "also", "it", "was", "have", "can", "please", "file", "project", "error", "successfully",
}
_TRANSLITERATION = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def detect_language(text: str) -> str:
    """Grobe Spracherkennung: 'de' oder 'en'."""
    if settings.RESPONSE_LANGUAGE in ("de", "en"):
        return settings.RESPONSE_LANGUAGE
    if re.search(r"[äöüÄÖÜß]", text):
        return "de"
    words = re.findall(r"[a-zA-ZäöüÄÖÜß]+", text.lower())
    de = sum(1 for w in words if w in _GERMAN_WORDS)
    en = sum(1 for w in words if w in _ENGLISH_WORDS)
    return "de" if de > en else "en"


def clean_text(text: str) -> str:
    """Bereitet Text für die Sprachausgabe auf. Umlaute bleiben erhalten."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`.*?`", "", text)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = re.sub(r"[*_#~>|<\[\]{}()]", "", text)
    text = re.sub(r"(?i)\b(tilde|atilde|asterisk|backslash|slash|hash|underscore|absatz)\b", "", text)
    text = unicodedata.normalize("NFC", text)
    # Nur Buchstaben (auch Umlaute), Ziffern, Leerzeichen und einfache Satzzeichen behalten
    text = re.sub(r"[^\w\s.,!?;:'\"-]|_", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class TextToSpeech:
    def __init__(self):
        self._lock = threading.Lock()
        self._pygame_init = False
        self._warned_missing_voice = False

    def speak(self, text: str):
        if not text.strip():
            return

        clean = clean_text(text)
        if not clean:
            return

        log.info("TTS: %s", clean[:200])
        threading.Thread(target=self._speak_worker, args=(clean,), daemon=True).start()

    def _choose_voice(self, text: str):
        """Gibt (Modellpfad, ggf. angepasster Text) zurück oder (None, text)."""
        lang = detect_language(text)
        model = ASSETS_DIR / VOICE_FILES[lang]
        if model.exists():
            return model, text
        if lang == "de":
            fallback = ASSETS_DIR / VOICE_FILES["en"]
            if fallback.exists():
                if not self._warned_missing_voice:
                    log.warning(
                        "German voice %s not found in %s - using the English voice instead. "
                        "Download the German Piper voice for natural output.",
                        VOICE_FILES["de"], ASSETS_DIR,
                    )
                    self._warned_missing_voice = True
                return fallback, text.translate(_TRANSLITERATION)
        log.error("No Piper voice found in %s", ASSETS_DIR)
        return None, text

    def _speak_worker(self, text: str):
        with self._lock:
            temp_wav = settings.DATA_DIR / "tts_output.wav"
            try:
                model, spoken = self._choose_voice(text)
                if model is None:
                    return

                cmd = ["piper", "--model", str(model), "--output_file", str(temp_wav)]
                process = subprocess.run(cmd, input=spoken.encode("utf-8"), capture_output=True)

                if process.returncode != 0:
                    log.error("Piper error: %s", process.stderr.decode("utf-8", errors="replace"))
                    return

                import pygame  # verzögert, damit das Modul auch ohne Audio importierbar bleibt

                if not self._pygame_init:
                    pygame.mixer.init()
                    self._pygame_init = True

                pygame.mixer.music.load(str(temp_wav))
                pygame.mixer.music.play()

                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)

                pygame.mixer.music.unload()

            except Exception as e:  # noqa: BLE001
                log.exception("TTS error: %s", e)
            finally:
                try:
                    if temp_wav.exists():
                        os.remove(temp_wav)
                except OSError:
                    pass
