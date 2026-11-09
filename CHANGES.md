# EVOX – Korrekturen (Phase 0/1 + Tool-Sicherheit)

Alle Dateien liegen im selben Pfad wie in deinem Repo. Kopiere sie über dein Projekt
(vorher `git tag vor-fixes` oder einen Branch anlegen).

**Nicht überschreiben:** alle `__init__.py` in deinem Projekt (ich habe sie nicht gesehen,
sie scheinen leer zu sein; `tests/__init__.py` ist dagegen neu und gehört zu den Tests).

## Dateien

| Datei | Status |
|---|---|
| `core/tool_result.py` | neu – Ergebnis mit ehrlichem Status (ok / unverified / failed) |
| `core/security.py` | neu – Pfadrichtlinie, Namensprüfung, Risikostufen, Schutz vor Prompt Injection |
| `core/logging_setup.py` | neu – Logdatei + Fehler aus Threads |
| `core/ollama_utils.py` | neu – prüft, ob das Modell installiert ist |
| `computer_control/app_launcher.py` | neu – Programme starten/schließen ohne Shell, mit Prüfung |
| `config/settings.py` | ersetzt |
| `core/tool_manager.py` | ersetzt |
| `core/ai_engine.py` | ersetzt |
| `computer_control/admin_tools.py` | ersetzt |
| `system_administration/project_generator.py` | ersetzt |
| `system_administration/file_manager.py` | ersetzt (nur `delete_project` neu, `win32com` verzögert importiert) |
| `system_administration/program_manager.py` | ersetzt (Programme ohne `EstimatedSize` bleiben in der Liste) |
| `internet/web_search.py` | ersetzt |
| `interface/backend.py` | ersetzt |
| `voice/text_to_speech.py` | ersetzt |
| `main.py` | ersetzt |
| `requirements.txt` | `duckduckgo-search` ergänzt |
| `requirements-dev.txt`, `tests/` | neu |

## Pflicht: eine Zeile in `interface/qml/main.qml`

Wenn die Zustimmung abläuft, schließt EVOX das Popup selbst. Dafür in der vorhandenen
`Connections { target: backend ... }` (dort, wo `onConfirmRequested` steht) ergänzen:

```qml
function onConfirmCancelled() { confirmPopup.close() }
```

Falls dein Popup eine andere `id` hat, diese verwenden. Ohne die Zeile läuft alles, nur
bleibt ein abgelaufenes Popup offen.

## Was jedes Problem jetzt macht

**1. Erfolgsmeldungen ohne Prüfung.**
Jedes Tool liefert ein `ToolResult`. Das Modell bekommt `SUCCESS (verified)`, `UNVERIFIED …` oder
`FAILED …`. „Action completed successfully, Sir.“ gibt es nicht mehr. Bleibt die Modellantwort leer,
erzeugt EVOX eine Zusammenfassung aus den echten Ergebnissen („1 von 2 Aktion(en) erfolgreich …,
Fehlgeschlagen: …“). Auch das Erreichen der Rundengrenze wird gemeldet (12 Runden statt 5, über
`settings.MAX_TOOL_ROUNDS` einstellbar). Programmstart zählt nur als erfolgreich, wenn ein passender
Prozess läuft; sonst `unverified`. Dateischreiben liest die Datei zurück und vergleicht.

**2. Geister-Tools.**
Die Tool-Definitionen werden aus der Registry erzeugt, ein Tool kann also nicht mehr angeboten, aber
nicht implementiert sein. `close_application` ist jetzt implementiert (erst sauber schließen,
`force=true` nur mit Zustimmung). `delete_project` und `uninstall_with_revo` sind angebunden.
`uninstall_program` gab es nirgends und ist entfernt.

**3. Sicherheit.**
- Kein `shell=True` mehr. Programmnamen werden validiert, `cmd`, `powershell`, `regedit` u. ä. sind gesperrt.
- Pfade: Schreiben ohne Rückfrage nur im Projektordner. Übriger Benutzerordner nur nach Zustimmung.
  Gesperrt: `AppData`, `.ssh`, Windows-/Programmordner, EVOX-Daten. Relative Pfade gelten im Projektordner.
  `..`, Symlinks, UNC-Pfade und `datei:stream` werden abgefangen.
  `.bat/.ps1/.exe/…` sowie `.git/` und `.vscode/` brauchen ebenfalls Zustimmung.
- `execute_admin_command` zerlegt den Befehl in Programm + Parameter (vorher wurde der ganze Text als
  Dateiname übergeben), prüft den Rückgabecode von Windows (abgelehntes UAC = Fehler) und erlaubt nur
  Programme aus `settings.ADMIN_ALLOWED_EXECUTABLES`.
- Das Popup zeigt jetzt Werkzeug, Risiko und Details (voller Pfad, Anzahl Dateien bei Löschungen).
- Zustimmung läuft nach 120 s ab und zählt dann als „abgelehnt“. `execute_tool` führt riskante Tools
  ohne `confirmed=True` nie aus, auch nicht bei direktem Aufruf.
- Texte aus Web, Dateien und Ordnerlisten werden als `<untrusted_data>` markiert; der System-Prompt
  verbietet, Anweisungen daraus zu befolgen.
- Neu gefunden und behoben: `create_web_project` überschrieb bestehende Projekte und akzeptierte
  `..\..` im Namen. `delete_project` hätte mit solchen Namen außerhalb des Projektordners gelöscht
  (jetzt geprüft, Links werden nicht angefasst).
- Vor dem Überschreiben einer Datei wird eine Kopie nach `%LOCALAPPDATA%\EVOX\Backups\` gelegt.

**4. Revo.**
Der erfundene Parameter `/uninstall` ist entfernt. EVOX prüft, ob das Programm installiert ist
(eindeutig?), startet Revo ohne Parameter und meldet ausdrücklich **unverified**: Die Deinstallation
selbst machst du in Revos Fenster. Eine echte Automatisierung braucht erst die Doku deiner Revo-Version
oder UI-Automation.

**5. Pfade und Packaging.**
Daten liegen unter `%LOCALAPPDATA%\EVOX\` (`Data`, `Logs`, `Backups`, `Config`, `Reports`).
Die alte `data/evox.db` wird beim ersten Start **kopiert** (nicht verschoben). Der Projektordner kommt
aus `settings.PROJECTS_DIR` (Standard: `Desktop\EVOX_Projects`, auch mit OneDrive-Desktop), nicht mehr
fest aus „alexa“. Logging schreibt nach `Logs\evox.log`, QML-Warnungen und unbehandelte Fehler auch.

**6. Prompt und Sprache.**
Korrektur zu meiner ersten Analyse: „Sir“ stand nicht im Prompt, sondern in den Rückfalltexten
(jetzt entfernt). Der Prompt verlangte aber Englisch. `settings.RESPONSE_LANGUAGE` steuert das nun
(`"auto"` = Sprache des Nutzers, Standard). Wichtig: Die Sprachausgabe nutzte nur eine englische Stimme
und löschte alle Umlaute. Jetzt bleiben Umlaute erhalten, und es wird je Text die passende Stimme
gewählt. **Für gutes Deutsch** lade eine deutsche Piper-Stimme (z. B. `de_DE-thorsten-high.onnx` und
`.onnx.json`) in den Ordner `assets/`. Ohne sie nimmt EVOX die englische Stimme und schreibt
Umlaute als ae/oe/ue. Wenn dir das nicht passt: `RESPONSE_LANGUAGE = "en"`.

**7. Backend.**
Ollama-Prüfung läuft im Hintergrund (kein Einfrieren beim Start), alle 30 s neu, und prüft jetzt, ob
`qwen3:14b` wirklich installiert ist. Funktionen, die der Worker-Thread auslöst, sind als `@Slot`
markiert und laufen damit sicher im Hauptthread.

**8. Sonstiges.**
`ProgramManager` ließ jedes Programm ohne `EstimatedSize` in der Registry stillschweigend aus der
Liste fallen. Das hätte das Revo-Tool fälschlich „nicht installiert“ melden lassen; jetzt bleiben
diese Programme drin (Größe 0).
`duckduckgo-search` stand nicht in der `requirements.txt`, obwohl `web_search.py` es importiert
(frische Installation wäre beim Start abgestürzt). Jetzt ergänzt, `ddgs` (neuer Name) wird auch
unterstützt. Streaming gibt es als Option (`STREAM_RESPONSES`), Standard **aus**. Tests: `tests/`.

## Tests ausführen

```
pip install -r requirements-dev.txt
python -m pytest tests          # oder: python -m unittest discover -s tests -t .
```
141 Tests; hier alle grün. Sie nutzen einen temporären Ordner und fassen deinen Benutzerordner nicht an.

## Was ich NICHT testen konnte (bitte einmal von Hand prüfen)

Die Logik ist getestet, aber nicht gegen echtes Windows, Qt und Ollama:

1. EVOX starten → Fenster öffnet, Status zeigt „Verbunden · qwen3:14b bereit“. Eintrag in `%LOCALAPPDATA%\EVOX\Logs\evox.log`.
2. Alter Chatverlauf und Gedächtnis sind noch da (Datenbank wurde kopiert).
3. „Öffne Notepad“ → Notepad geht auf, Antwort sagt „verified“. „Öffne Quatschprogramm“ → Fehler statt Erfolg.
4. „Schließe Notepad“ mit offenem, ungespeichertem Text → es bleibt offen, EVOX meldet das.
5. „Erstelle eine Datei test.txt“ → liegt in `Desktop\EVOX_Projects`, öffnet in VS Code.
6. „Lösche das Projekt X“ → Popup mit vollem Pfad; „Nein“ löscht nichts; Popup nicht beantworten → schließt nach 2 Minuten (braucht die QML-Zeile).
7. „Führe `sfc /scannow` als Admin aus“ → UAC erscheint; UAC ablehnen → Fehler.
8. Revo-Tool: Revo startet, Antwort sagt ausdrücklich, dass die Deinstallation dort passiert.
9. Sprachausgabe: Umlaute werden gesprochen (mit deutscher Stimme natürlich).

## Noch offen (nicht Teil dieser Korrekturen)

- `speech_to_text.py`: Whisper wird beim Start synchron geladen (verzögert den Start), das Mikrofon
  ist fest auf Index 2 gesetzt, und `stop_recording` transkribiert im UI-Thread (kurzes Einfrieren).
- `pc_control.py`, `ui_automation.py`, `disk_analyzer.py`, `report_generator.py`, `memory/*` und die
  QML-Dateien habe ich nicht auf Sicherheit/Fehler geprüft.
- Eine echte Revo-Automatisierung und die Admin-Hilfsdienst-Architektur aus dem Master-Prompt.
