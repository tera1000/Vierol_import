"""
Ingest: Dateisystem-Seite der Pipeline.

Grundsatz "jede Datei bekommt ein definiertes Ende": Nach der
Verarbeitung liegt jede Datei aus `data/ingest/` entweder in
`data/archive/<quelle>/` (erfolgreich geladen) oder in `data/reject/`
mit einem daneben liegenden `.reject.txt`-Bericht (warum abgelehnt).

Design-Entscheidungen:

  1. Zeitstempel im Zielnamen: `<original>__<yyyymmdd_hhmmss>.<ext>`.
     Verhindert Namenskollisionen, wenn dieselbe Datei mehrfach
     geliefert wird (im echten Betrieb der Regelfall).
  2. Archiv nach Quelle sortiert: erleichtert spaeteres Nachverfolgen
     ("was hat Topmotive letzten Monat geliefert?").
  3. Reject-Bericht als Klartext-Datei, nicht in einer Queue-DB — der
     User (Fachbereich) muss die Datei mit Notepad oeffnen koennen,
     um zu sehen, was schiefging.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)


def scanne_ingest(ingest_dir: Path) -> list[Path]:
    """Alle regulaeren Dateien im Ingest-Verzeichnis auflisten (sortiert).

    Unterverzeichnisse und versteckte Dateien werden ignoriert."""
    if not ingest_dir.exists():
        return []
    dateien = [
        p for p in sorted(ingest_dir.iterdir())
        if p.is_file() and not p.name.startswith(".")
    ]
    logger.info("Ingest-Scan %s: %d Datei(en) gefunden.", ingest_dir, len(dateien))
    return dateien


def verschiebe_ins_archiv(
    datei: Path, archive_dir: Path, quelle: str, zeitstempel: datetime | None = None
) -> Path:
    """Erfolgreich verarbeitete Datei nach `archive/<quelle>/` verschieben."""
    zeit = zeitstempel or datetime.now()
    ziel_dir = archive_dir / quelle
    ziel_dir.mkdir(parents=True, exist_ok=True)
    zielpfad = ziel_dir / _mit_zeitstempel(datei.name, zeit)
    shutil.move(str(datei), str(zielpfad))
    logger.info("Archiviert: %s -> %s", datei.name, zielpfad)
    return zielpfad


def verschiebe_ins_reject(
    datei: Path,
    reject_dir: Path,
    grund: str,
    details: list[str] | None = None,
    zeitstempel: datetime | None = None,
) -> Path:
    """Abgelehnte Datei nach `reject/` verschieben und Bericht daneben ablegen.

    Der Bericht (.reject.txt) hat denselben Basename wie die Datei —
    so gehoeren Datei und Bericht sichtbar zusammen.
    """
    zeit = zeitstempel or datetime.now()
    reject_dir.mkdir(parents=True, exist_ok=True)
    zielname = _mit_zeitstempel(datei.name, zeit)
    zielpfad = reject_dir / zielname
    shutil.move(str(datei), str(zielpfad))

    bericht = zielpfad.with_suffix(zielpfad.suffix + ".reject.txt")
    bericht.write_text(
        _bericht_text(datei.name, zeit, grund, details or []),
        encoding="utf-8",
    )
    logger.info("Rejected: %s -> %s (Grund: %s)", datei.name, zielpfad, grund)
    return zielpfad


def _mit_zeitstempel(name: str, zeit: datetime) -> str:
    stamm, punkt, endung = name.rpartition(".")
    if not punkt:
        return f"{name}__{zeit:%Y%m%d_%H%M%S}"
    return f"{stamm}__{zeit:%Y%m%d_%H%M%S}.{endung}"


def _bericht_text(
    original_name: str, zeit: datetime, grund: str, details: list[str]
) -> str:
    zeilen = [
        f"Datei:     {original_name}",
        f"Rejected:  {zeit:%Y-%m-%d %H:%M:%S}",
        f"Grund:     {grund}",
        "",
    ]
    if details:
        zeilen.append("Details:")
        zeilen.extend(f"  - {d}" for d in details)
    return "\n".join(zeilen) + "\n"


# --- watch-Modus (dauerhafte Verzeichnisueberwachung) ------------------------


class _IngestHandler(FileSystemEventHandler):
    """Reagiert auf neue Dateien im Ingest-Verzeichnis.

    Zwei Ereignisse loesen aus:
      * created — Datei komplett neu geschrieben (z. B. per Editor)
      * moved   — Datei per rename/move in den Ordner geschoben
                  (das ist der Standardweg bei FTP/SMB, weil dabei
                  atomar aus einem temporaeren Namen umbenannt wird)

    Der `_stabil_warten`-Trick verhindert, dass wir eine Datei
    anfassen, waehrend sie noch geschrieben wird — wir warten, bis
    ihre Groesse zwei Ticks lang gleich bleibt.
    """

    def __init__(self, callback: Callable[[Path], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        self._verarbeite(Path(event.src_path))

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        self._verarbeite(Path(event.dest_path))

    def _verarbeite(self, pfad: Path) -> None:
        if not pfad.exists() or pfad.name.startswith("."):
            return
        if not _stabil_warten(pfad):
            logger.warning("Datei %s wurde nicht stabil — uebersprungen.", pfad.name)
            return
        try:
            self._callback(pfad)
        except Exception:
            logger.exception("Fehler beim Verarbeiten von %s", pfad.name)


def _stabil_warten(
    pfad: Path, ticks: int = 3, intervall: float = 0.5
) -> bool:
    """Warten, bis die Dateigroesse in aufeinanderfolgenden Ticks
    unveraendert bleibt — dann ist der Schreibvorgang durch."""
    letzte = -1
    for _ in range(ticks):
        try:
            aktuelle = pfad.stat().st_size
        except FileNotFoundError:
            return False
        if aktuelle == letzte:
            return True
        letzte = aktuelle
        time.sleep(intervall)
    return False


def starte_watch(
    ingest_dir: Path, callback: Callable[[Path], None]
) -> BaseObserver:
    """Beobachter starten (nicht-blockierend). Aufrufer ist fuer Stop/join zustaendig."""
    ingest_dir.mkdir(parents=True, exist_ok=True)
    handler = _IngestHandler(callback)
    observer = Observer()
    observer.schedule(handler, str(ingest_dir), recursive=False)
    observer.start()
    logger.info("Watch aktiv auf %s", ingest_dir)
    return observer


# --- Quarantaene (partieller Modus) ------------------------------------------


def schreibe_quarantaene(
    original_datei: Path,
    reject_dir: Path,
    fehler_zeilen: list["tuple[int, list[str], str]"],
    zeitstempel: datetime | None = None,
) -> Path:
    """Schreibt eine Quarantaene-CSV neben dem Reject-Verzeichnis.

    Format der CSV:
        original_zeile_nr; fehler_grund; original_zeile
    (Semikolon-getrennt, unabhaengig vom Trennzeichen der Quelldatei —
     der Fachbereich soll das in Excel oeffnen koennen.)

    `fehler_zeilen`: Liste aus (Zeilennummer, Rohzeile-Spalten, Grund).
    """
    import csv as _csv

    zeit = zeitstempel or datetime.now()
    reject_dir.mkdir(parents=True, exist_ok=True)
    # Namensschema: <original_stamm>_fehlerhaft__<zeitstempel>.csv
    # Der Zeitstempel bleibt drin, damit bei mehreren Laeufen derselben
    # Quelle nichts ueberschrieben wird.
    ziel = reject_dir / _mit_zeitstempel(
        f"{original_datei.stem}_fehlerhaft.csv", zeit
    )

    with open(ziel, "w", newline="", encoding="utf-8") as f:
        writer = _csv.writer(f, delimiter=";")
        writer.writerow(["zeile_nr", "fehler_grund", "original_zeile"])
        for nr, spalten, grund in fehler_zeilen:
            # Rohzeile mit dem Original-Trennzeichen wieder zusammensetzen
            # waere umstaendlich — wir schreiben sie einfach mit Pipe, damit
            # sie in Excel als ein Feld erscheint.
            original = "|".join(spalten)
            writer.writerow([nr, grund, original])

    logger.info(
        "Quarantaene geschrieben: %s (%d Zeilen)", ziel, len(fehler_zeilen)
    )
    return ziel