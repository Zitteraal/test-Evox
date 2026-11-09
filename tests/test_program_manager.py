import importlib
import sys
import types
import unittest
from unittest import mock


def make_fake_winreg(entries):
    """entries: Liste von dicts (Werte eines Uninstall-Unterschlüssels) für HKLM\\...\\Uninstall."""
    mod = types.ModuleType("winreg")
    mod.HKEY_LOCAL_MACHINE = "HKLM"
    mod.HKEY_CURRENT_USER = "HKCU"
    uninstall_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    class Key:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(parent, sub):
        if parent == "HKLM" and sub == uninstall_path:
            return Key({"entries": entries})
        if isinstance(parent, Key) and isinstance(sub, str) and sub.startswith("k"):
            return Key({"values": entries[int(sub[1:])]})
        raise OSError("not found")

    def query_info(key):
        return (len(key.data["entries"]), 0, 0)

    def enum_key(key, i):
        return f"k{i}"

    def query_value(key, name):
        values = key.data["values"]
        if name not in values:
            raise FileNotFoundError(name)
        return (values[name], 1)

    mod.OpenKey = open_key
    mod.QueryInfoKey = query_info
    mod.EnumKey = enum_key
    mod.QueryValueEx = query_value
    return mod


class ProgramManagerTests(unittest.TestCase):
    def load(self, entries):
        fake = make_fake_winreg(entries)
        patcher = mock.patch.dict(sys.modules, {"winreg": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("system_administration.program_manager", None)
        module = importlib.import_module("system_administration.program_manager")
        self.addCleanup(lambda: sys.modules.pop("system_administration.program_manager", None))
        return module.ProgramManager

    def test_program_without_estimated_size_is_kept(self):
        pm = self.load([
            {"DisplayName": "Big App", "EstimatedSize": 2 * 1024 ** 2, "UninstallString": "big.exe /u"},
            {"DisplayName": "No Size App", "UninstallString": "nosize.exe /u"},
        ])
        names = [p["name"] for p in pm.get_installed_programs()]
        self.assertEqual(names, ["Big App", "No Size App"])  # nach Größe sortiert, beide vorhanden
        self.assertEqual(pm.get_installed_programs()[1]["size_gb"], 0.0)

    def test_entries_without_name_or_uninstall_string_are_skipped(self):
        pm = self.load([
            {"EstimatedSize": 5, "UninstallString": "x"},
            {"DisplayName": "No Uninstall", "EstimatedSize": 5},
            {"DisplayName": "Good", "UninstallString": "good /u"},
        ])
        self.assertEqual([p["name"] for p in pm.get_installed_programs()], ["Good"])

    def test_find_program_is_case_insensitive_partial(self):
        pm = self.load([{"DisplayName": "Revo Uninstaller 2.5", "UninstallString": "u"}])
        self.assertEqual(len(pm.find_program("revo")), 1)
        self.assertEqual(pm.find_program("zzz"), [])


if __name__ == "__main__":
    unittest.main()
