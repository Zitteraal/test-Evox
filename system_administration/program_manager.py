import winreg


class ProgramManager:
    @staticmethod
    def get_installed_programs() -> list:
        """Liest installierte Programme aus der Windows-Registrierung."""
        programs = []
        # Die Registry-Pfade für 64-bit und 32-bit Programme
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
        ]

        for hkey, path in reg_paths:
            try:
                with winreg.OpenKey(hkey, path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                except FileNotFoundError:
                                    continue

                                # Größe in KB auslesen und in GB umrechnen.
                                # Viele Programme tragen keine Größe ein - sie dürfen deshalb
                                # nicht aus der Liste fallen (Größe dann 0).
                                size_gb = 0.0
                                try:
                                    size_kb = winreg.QueryValueEx(subkey, "EstimatedSize")[0]
                                    size_gb = round(size_kb / (1024**2), 2)
                                except (FileNotFoundError, TypeError):
                                    pass

                                uninstall_string = ""
                                try:
                                    uninstall_string = winreg.QueryValueEx(subkey, "UninstallString")[0]
                                except FileNotFoundError:
                                    pass

                                if name and uninstall_string:  # Nur Programme mit Deinstallationsbefehl
                                    programs.append({
                                        "name": name,
                                        "size_gb": size_gb,
                                        "uninstall_string": uninstall_string
                                    })
                        except OSError:
                            continue
            except OSError:
                continue

        # Duplikate filtern und nach Größe sortieren
        unique_programs = {p['name']: p for p in programs}.values()
        sorted_programs = sorted(unique_programs, key=lambda x: x['size_gb'], reverse=True)
        return list(sorted_programs)

    @staticmethod
    def find_program(query: str) -> list:
        """Sucht ein Programm anhand eines Namens oder Teilnamens."""
        programs = ProgramManager.get_installed_programs()
        query = query.lower().strip()
        # Findet alle Programme, die den Suchbegriff im Namen haben
        return [p for p in programs if query in p['name'].lower()]
