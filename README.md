# Vierol Import — Prototyp Bachelorarbeit

Metadaten-gesteuerte Importarchitektur für heterogene externe Datenquellen.

## Aufbau

- `src/vierol_import/` — Pipeline-Code (ein Modul pro Verarbeitungsstufe)
- `config_catalog/` — Zentraler Konfigurations-Katalog (YAML-Dateien)
- `data/ingest/` — Beobachtetes Eingangsverzeichnis
- `data/archive/` — Erfolgreich verarbeitete Dateien
- `data/reject/` — Fehlgeschlagene Verarbeitung mit Fehlerbericht
- `data/samples/` — Beispieldateien für Tests und Vorführung
- `tests/` — Automatisierte Tests

## Setup (einmalig)

### Voraussetzungen

- Python 3.11 oder 3.12
- Git
- VS Code

### Schritte

```bash
# 1. In das Projektverzeichnis wechseln
cd vierol_import

# 2. Virtuelle Umgebung erstellen
python -m venv .venv

# 3. Umgebung aktivieren
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

# 4. Projekt und Abhängigkeiten installieren
pip install -e ".[dev]"

# 5. Prüfen, ob alles läuft
python -m vierol_import --help
```

## Erster Testlauf

```bash
# Beispieldatei ins Ingest-Verzeichnis kopieren
cp data/samples/warenkorb_beispiel.csv data/ingest/

# Pipeline starten
python -m vierol_import run

# Ergebnis prüfen
ls data/archive/    # sollte die verarbeitete Datei enthalten
```

## Struktur der Konfigurationsdateien

Jede YAML-Datei im `config_catalog/` beschreibt einen Inhaltstyp mit den Abschnitten:

- `klassifikation` — Wie wird die Datei erkannt?
- `parsing` — Wie wird die CSV gelesen?
- `validierung` — Welche Regeln gelten für die Felder?
- `mapping` — Wie werden Quellfelder auf das kanonische Modell abgebildet?
- `zielsystem` — Wohin werden die Daten geladen?

Siehe `config_catalog/warenkorb.yaml` als Referenz.
