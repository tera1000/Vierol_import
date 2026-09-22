# Vierol Import — Prototyp Bachelorarbeit

Metadaten-gesteuerte Importarchitektur für heterogene externe Datenquellen.

## Aufbau

```
vierol_import/
├── src/vierol_import/
│   ├── engine.py                  # ImportEngine: orchestriert Klassifikation,
│   │                               # Validierung, Mapping, Load
│   ├── classification/
│   │   └── classifier.py          # Klassifikations-Score
│   ├── validation/
│   │   └── validator.py           # Datensatzweise Validierung
│   ├── mapping/
│   │   └── mapper.py              # Vier Feldkategorien (Quelle/Auto/System/Berechnet)
│   ├── loading/
│   │   ├── loader.py              # Dispatcher + SQLite-Adapter
│   │   └── oracle_loader.py       # Oracle-Adapter
│   ├── catalog/
│   │   ├── reader.py              # Laedt und validiert den Konfigurationskatalog
│   │   ├── meta_schema.py         # Deklaratives Meta-Schema (Pydantic)
│   │   ├── datei_reader.py        # Format-Abstraktion (CSV/Excel/XML/JSON)
│   │   └── verbindung_registry.py # Laedt zielsysteme/*.yaml
│   ├── ingest/
│   │   ├── zip_entpacker.py       # Rekursives ZIP-Entpacken
│   │   └── watcher.py             # Verzeichnis-Scan fuer --ordner/--pfad
│   ├── monitoring/
│   │   ├── audit_log.py           # Log-Tabelle (import_lauf, import_fehler)
│   │   └── logger.py              # Logging-Setup
│   ├── typen.py                   # Zentrale Typkonvertierung/-pruefung
│   ├── encoding_erkennung.py      # Automatische Zeichenkodierungs-Erkennung
│   └── main.py                    # CLI-Befehle
├── config_catalog/                # Quellen-Konfigurationen (YAML, versioniert)
├── zielsysteme/                   # Verbindungsdefinitionen (OHNE Zugangsdaten)
└── tests/                         # Automatisierte Tests
```

## Setup (einmalig)

### Voraussetzungen

- Python 3.11 oder 3.12
- Git
- VS Code (oder ein anderer Editor)
- Für Oracle-Zielsysteme: kein separater Oracle Instant Client noetig, `oracledb` laeuft im Thin-Modus

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

# 4. Projekt und Abhaengigkeiten installieren
pip install -e ".[dev]"

# 5. Pruefen, ob alles laeuft
python -m vierol_import --help
```

**Wichtige Abhaengigkeiten** (werden automatisch mitinstalliert):

| Paket | Wofuer |
|---|---|
| `pydantic` | Meta-Schema und Validierung der Konfigurationen |
| `PyYAML` | Einlesen der YAML-Configs |
| `click` | Kommandozeilenbefehle |
| `oracledb` | Oracle-Anbindung (Thin-Client) |
| `pandas`, `openpyxl`, `lxml` | Excel-, XML- und JSON-Unterstuetzung |

## Verbindung einrichten (vor dem ersten Lauf notwendig)

Jede Config verweist auf eine Verbindung in `zielsysteme/`. Ohne mindestens eine gueltige Verbindungsdatei startet **kein** Befehl, der eine Datei verarbeitet.

**SQLite (fuer lokale Tests, kein Passwort noetig):**

```yaml
# zielsysteme/lokal_test.yaml
name: lokal_test
typ: sqlite
pfad: data/vierol_import.sqlite
```

**Oracle (Produktivsystem):**

```yaml
# zielsysteme/vierol_oracle_prod.yaml
name: vierol_oracle_prod
typ: oracle
user: <benutzername>
dsn: <host>:<port>/<service_name>
```

Das Passwort wird **nie** in der Datei gespeichert, sondern bei jedem Lauf interaktiv per `getpass` abgefragt und nur im Arbeitsspeicher gehalten.

Verbindung testen, bevor sie in einer Config referenziert wird:

```bash
python -m vierol_import connection-test lokal_test
python -m vierol_import list-connections
```

## Erster Testlauf

```bash
# Beispieldatei ins Ingest-Verzeichnis kopieren
cp data/samples/warenkorb_beispiel.csv data/ingest/ # oder cp data/samples/test.csv data/ingest/

# Konfigurationen gegen das Meta-Schema pruefen
python -m vierol_import validate-config

# Pipeline starten (fragt einmalig nach der passenden Quelle)
python -m vierol_import run --ordner data/ingest

# Ergebnis pruefen
python -m vierol_import zeige-log
```

## Wichtige Befehle im Überblick

| Befehl | Zweck |
|---|---|
| `run --file <datei>` | Eine einzelne Datei interaktiv verarbeiten |
| `run --ordner <pfad>` | Alle Dateien in einem Verzeichnis verarbeiten (fragt einmalig nach der Quelle) |
| `run --zip <archiv>` | ZIP-Archiv (auch verschachtelt) entpacken und verarbeiten |
| `run --pfad <netzlaufwerk>` | Wie `--ordner`, aber fuer Netzlaufwerke |
| `run ... --quelle <name>` | Klassifikation ueberspringen, Config fest vorgeben |
| `run ... --ja` | Ohne Rueckfragen durchlaufen (auch unsichere Zuordnungen) |
| `run ... --strategie skip\|update\|append` | Ladestrategie fuer diesen Lauf ueberschreiben |
| `dry-run --config <name> --file <datei>` | Volle Pipeline OHNE Datenbank-Schreibvorgang testen |
| `inspect <datei>` | Datei anschauen (Encoding, Trennzeichen, erste Zeilen) ohne sie zu verarbeiten |
| `validate-config` | Alle Configs im Katalog gegen das Meta-Schema pruefen |
| `export-schema` | JSON-Schema fuer VS-Code-Autocomplete exportieren |
| `zeige-log` | Letzte Verarbeitungslaeufe anzeigen |
| `zeige-fehler --lauf <ID>` | Detail-Fehler eines Laufs anzeigen (Zeile, Spalte, Wert, Original-Zeile) |
| `list-configs` | Alle Quellen-Configs mit Nutzungsstatistik anzeigen |
| `list-connections` | Alle registrierten Verbindungen anzeigen |
| `connection-test <name>` | Verbindung testweise aufbauen |
| `connection-delete <name>` | Verbindung loeschen (mit Referenz-Check) |

## Struktur der Konfigurationsdateien

Jede YAML-Datei im `config_catalog/` beschreibt einen Inhaltstyp. Der Dateiname (ohne Endung) muss mit dem `name`-Feld übereinstimmen.

```yaml
name: us_oe_oracle_test

# Wie wird die Rohdatei gelesen?
datei:
  format: auto              # auto | csv | excel | xml | json
  trennzeichen: "|"
  hat_header: false
  encoding: cp1252
  quoting: none              # minimal (Standard-CSV) | none (Anfuehrungszeichen sind normale Zeichen)

# Wie wird die Datei erkannt?
klassifikation:
  schwellenwert: 0.6
  stichprobe_zeilen: 20

# Welche Spalten hat die Quelldatei, und welche Regeln gelten?
spalten:
  - {position: 0, name: hersteller, typ: string, max_laenge: 200}
  - {position: 1, name: oeno, typ: string, pflicht: true}
  - {position: 5, name: ersatzflag, typ: string, max_laenge: 1, muster: "^[A-Z]?$"}

# Wie werden Quellfelder auf das kanonische Modell abgebildet?
mapping:
  felder:
    - {ziel: hersteller, kategorie: quelle, von: hersteller}
    - {ziel: jahrmonat, kategorie: system, system_quelle: pfad_regex,
       regex: "/(\\d{4})-(\\d{2})-\\d{2}", gruppen: [1, 2]}

# Wohin werden die Daten geladen?
zielsystem:
  ref: vierol_oracle_prod     # Name aus zielsysteme/*.yaml
  tabelle: DWHRR.US_OE_BRUTTOPREISE_TEST
  pk_konflikt: skip            # skip | update | insert
  upsert_key: [hersteller, oeno, jahrmonat]
  fehler_schwelle: 0.05        # max. 5% fehlerhafte Zeilen toleriert
```

Siehe `config_catalog/` für weitere Referenz-Configs.

### Hinweise zu einzelnen Feldern

- **`quoting: none`** ist wichtig für pipe-getrennte Dateien, in denen `"` als normales Freitext-Zeichen vorkommt (nicht als Feld-Begrenzer wie im klassischen CSV). Ohne diese Einstellung kann eine einzelne Zeile mit `"` mehrere nachfolgende Datensätze fälschlich in ein Feld verketten.
- **`max_laenge`** an einer Spalte wird nicht nur bei der Validierung genutzt, sondern auch dem Oracle-Adapter als Bind-Größe mitgegeben. Das verhindert, dass ein einzelner überlanger Wert in einem Batch andere, korrekte Werte derselben Spalte fälschlich als „zu lang" ablehnen lässt.
- **`upsert_key`** muss ein fachlicher Schlüssel sein (z. B. Hersteller + Artikelnummer + Zeitraum), kein technischer Datenbank-Primärschlüssel.

## Tests ausführen

```bash
python -m pytest tests/ -v
```

## Duplikatsprüfung bei großen Zieltabellen

Der SELECT-basierte Duplikatsschutz (`pk_konflikt: skip` oder `update`) benötigt bei Zieltabellen mit mehreren Millionen Zeilen einen Datenbankindex auf den `upsert_key`, sonst führt jeder Lauf zu einem vollständigen Tabellenscan:

```sql
CREATE INDEX idx_<tabelle>_bk ON <schema>.<tabelle> (<spalte1>, <spalte2>, ...);
```

## Bekannte Grenzen

- Excel-, XML- und JSON-Dateien werden vollständig in den Arbeitsspeicher geladen (kein Streaming, anders als bei CSV/TXT)
- Keine gleichzeitige Verteilung einer Quelle an mehrere Zielsysteme
- Keine automatisierte, unbeaufsichtigte Verarbeitung vorgesehen (externe Lieferungen sind passwortgeschützt und erfordern manuelle Vorbereitung)

Details siehe Kapitel 6 (Evaluation) und Kapitel 7 (Ausblick) der Bachelorarbeit.