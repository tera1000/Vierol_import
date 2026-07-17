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
from datetime import datetime
from pathlib import Path

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