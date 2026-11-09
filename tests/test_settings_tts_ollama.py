import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import settings
from core.ollama_utils import model_is_installed, normalize_model_name
from voice import text_to_speech as tts


class SettingsTests(unittest.TestCase):
    def test_user_data_is_outside_the_program_folder(self):
        for p in (settings.DATA_DIR, settings.LOGS_DIR, settings.DB_PATH, settings.BACKUP_DIR):
            self.assertTrue(str(p).startswith(str(settings.USER_DATA_DIR)), p)
        self.assertNotIn(str(settings.BASE_DIR / "data"), str(settings.DB_PATH))
        self.assertTrue(settings.DATA_DIR.is_dir() and settings.LOGS_DIR.is_dir())

    def test_all_names_used_by_other_modules_still_exist(self):
        for name in ("BASE_DIR", "DATA_DIR", "LOGS_DIR", "DB_PATH", "OLLAMA_URL", "OLLAMA_CHAT_ENDPOINT",
                     "OLLAMA_TAGS_ENDPOINT", "OLLAMA_MODEL", "APP_NAME", "APP_SLOGAN", "APP_VERSION",
                     "MEMORY_EXTRACTION_ENABLED", "MEMORY_MIN_USER_LEN", "SYSTEM_PROMPT"):
            self.assertTrue(hasattr(settings, name), name)
        self.assertEqual(settings.OLLAMA_MODEL, "qwen3:14b")

    def test_system_prompt(self):
        prompt = settings.SYSTEM_PROMPT
        self.assertNotIn("alexa", prompt)
        self.assertNotIn("Sir", prompt)
        self.assertNotIn("ALWAYS RESPOND IN ENGLISH", prompt)
        self.assertIn(settings.PROJECTS_DIR.as_posix(), prompt)
        self.assertIn("language the user writes in", prompt)
        self.assertIn("untrusted_data", prompt)
        self.assertIn("UNVERIFIED", prompt)


class SettingsReloadTests(unittest.TestCase):
    """Lädt settings mit anderen Umgebungsvariablen neu und stellt danach den alten Zustand her."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("EVOX_HOME", "EVOX_PROJECTS_DIR")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if hasattr(sys, "frozen"):
            del sys.frozen
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS
        importlib.reload(settings)

    def test_legacy_database_is_copied_not_moved(self):
        base = Path(tempfile.mkdtemp())
        (base / "data").mkdir()
        legacy = base / "data" / "evox.db"
        legacy.write_bytes(b"SQLite legacy content")
        os.environ["EVOX_HOME"] = str(Path(tempfile.mkdtemp()) / "home")
        sys.frozen = True
        sys._MEIPASS = str(base)
        importlib.reload(settings)

        self.assertTrue(settings.LEGACY_DB_MIGRATED)
        self.assertEqual(settings.DB_PATH.read_bytes(), b"SQLite legacy content")
        self.assertTrue(legacy.exists(), "the original must stay as a backup")

    def test_existing_new_database_is_never_overwritten(self):
        base = Path(tempfile.mkdtemp())
        (base / "data").mkdir()
        (base / "data" / "evox.db").write_bytes(b"old")
        home = Path(tempfile.mkdtemp()) / "home"
        (home / "Data").mkdir(parents=True)
        (home / "Data" / "evox.db").write_bytes(b"new and precious")
        os.environ["EVOX_HOME"] = str(home)
        sys.frozen = True
        sys._MEIPASS = str(base)
        importlib.reload(settings)

        self.assertFalse(settings.LEGACY_DB_MIGRATED)
        self.assertEqual(settings.DB_PATH.read_bytes(), b"new and precious")

    def test_projects_dir_and_language_rule_follow_configuration(self):
        os.environ["EVOX_PROJECTS_DIR"] = str(Path(tempfile.mkdtemp()) / "My Projects")
        os.environ["EVOX_HOME"] = str(Path(tempfile.mkdtemp()) / "h")
        importlib.reload(settings)
        self.assertEqual(settings.PROJECTS_DIR.name, "My Projects")
        self.assertIn("My Projects", settings.SYSTEM_PROMPT)

    def test_localappdata_is_used_when_no_override(self):
        os.environ.pop("EVOX_HOME", None)
        fake = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(fake)}):
            importlib.reload(settings)
            self.assertEqual(settings.USER_DATA_DIR, fake / "EVOX")


class OllamaUtilsTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_model_name("Qwen3:14B"), "qwen3:14b")
        self.assertEqual(normalize_model_name("llama3"), "llama3:latest")

    def test_installed_detection(self):
        installed = ["llama3:latest", "qwen3:14b", ""]
        self.assertTrue(model_is_installed("qwen3:14b", installed))
        self.assertTrue(model_is_installed("llama3", installed))
        self.assertFalse(model_is_installed("qwen3:32b", installed))
        self.assertFalse(model_is_installed("qwen3", installed))
        self.assertFalse(model_is_installed("qwen3:14b", []))


class TtsTextTests(unittest.TestCase):
    def setUp(self):
        self._lang = settings.RESPONSE_LANGUAGE
        settings.RESPONSE_LANGUAGE = "auto"
        self.addCleanup(lambda: setattr(settings, "RESPONSE_LANGUAGE", self._lang))

    def test_clean_text_keeps_umlauts(self):
        cleaned = tts.clean_text("Schön, dass die Datei für dich fertig ist: Größe 3 KB!")
        self.assertIn("Schön", cleaned)
        self.assertIn("für", cleaned)
        self.assertIn("Größe", cleaned)

    def test_clean_text_removes_code_markup_and_symbols(self):
        cleaned = tts.clean_text("Fertig ✅ **fett** `code` ```\nprint(1)\n``` ende 🚀")
        self.assertNotIn("print", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("✅", cleaned)
        self.assertNotIn("🚀", cleaned)
        self.assertIn("Fertig", cleaned)
        self.assertIn("ende", cleaned)

    def test_clean_text_empty(self):
        self.assertEqual(tts.clean_text("```x```"), "")

    def test_language_detection(self):
        self.assertEqual(tts.detect_language("Die Datei wurde nicht gefunden und der Fehler ist klar."), "de")
        self.assertEqual(tts.detect_language("Das ist schön"), "de")
        self.assertEqual(tts.detect_language("The file was not found and the error is clear."), "en")
        self.assertEqual(tts.detect_language("OK"), "en")

    def test_language_setting_overrides_detection(self):
        settings.RESPONSE_LANGUAGE = "en"
        self.assertEqual(tts.detect_language("Das ist schön"), "en")
        settings.RESPONSE_LANGUAGE = "de"
        self.assertEqual(tts.detect_language("The file"), "de")

    def test_voice_choice_prefers_matching_voice(self):
        assets = Path(tempfile.mkdtemp())
        (assets / tts.VOICE_FILES["en"]).write_text("x")
        (assets / tts.VOICE_FILES["de"]).write_text("x")
        with mock.patch.object(tts, "ASSETS_DIR", assets):
            engine = tts.TextToSpeech()
            model, text = engine._choose_voice("Das ist schön und die Datei ist fertig")
        self.assertEqual(model.name, tts.VOICE_FILES["de"])
        self.assertIn("schön", text)

    def test_missing_german_voice_falls_back_to_english_with_transliteration(self):
        assets = Path(tempfile.mkdtemp())
        (assets / tts.VOICE_FILES["en"]).write_text("x")
        with mock.patch.object(tts, "ASSETS_DIR", assets):
            engine = tts.TextToSpeech()
            model, text = engine._choose_voice("Schön für dich, Größe")
        self.assertEqual(model.name, tts.VOICE_FILES["en"])
        self.assertEqual(text, "Schoen fuer dich, Groesse")

    def test_no_voice_at_all(self):
        with mock.patch.object(tts, "ASSETS_DIR", Path(tempfile.mkdtemp())):
            model, _ = tts.TextToSpeech()._choose_voice("Hello there")
        self.assertIsNone(model)

    def test_speak_ignores_empty_text(self):
        with mock.patch.object(tts.threading, "Thread") as thread:
            tts.TextToSpeech().speak("   ")
            tts.TextToSpeech().speak("```only code```")
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
