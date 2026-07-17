"""
Zentrale Import-Engine.

Orchestriert die Verarbeitungsstufen fuer jede eingehende Datei:
Klassifikation -> Validierung -> Mapping -> Load -> Nachbereitung.

Die Engine kennt die einzelnen Stufen als abstrakte Aufgaben und
delegiert die Ausfuehrung an die Fachmodule. Sie enthaelt bewusst
KEINE quellenspezifische Logik.
"""

import logging
import shutil
from pathlib import Path

from vierol_import.catalog.reader import CatalogReader

logger = logging.getLogger(__name__)


class ImportEngine:
    """
    Fuehrt die metadaten-gesteuerte Verarbeitung fuer eine oder mehrere Dateien aus.
    """

    def __init__(self, catalog_dir: Path) -> None:
        self.catalog = CatalogReader(catalog_dir)
        self.archive_dir = Path("data/archive")
        self.reject_dir = Path("data/reject")

    def process_directory(self, ingest_dir: Path) -> None:
        """Alle Dateien im Ingest-Verzeichnis einmal verarbeiten."""
        files = sorted(p for p in ingest_dir.iterdir() if p.is_file())
        if not files:
            logger.info("Keine Dateien im Ingest-Verzeichnis %s gefunden.", ingest_dir)
            return

        for file_path in files:
            self.process_file(file_path)

    def process_file(self, file_path: Path) -> None:
        """
        Eine einzelne Datei durch die Pipeline schicken.

        Ablauf:
            1. Klassifikation — welche Konfiguration passt?
            2. Validierung — entspricht die Datei ihrem Schema?
            3. Mapping — auf das kanonische Modell uebersetzen
            4. Load — ins Zielsystem schreiben
            5. Nachbereitung — Datei archivieren oder rejecten
        """
        logger.info("Verarbeitung startet: %s", file_path.name)

        try:
            # TODO Schritt 1: Klassifikation
            # config = self._classify(file_path)
            #
            # TODO Schritt 2: Validierung
            # validation_result = self._validate(file_path, config)
            # if not validation_result.ok:
            #     self._reject(file_path, validation_result.errors)
            #     return
            #
            # TODO Schritt 3: Mapping
            # mapped = self._map(file_path, config)
            #
            # TODO Schritt 4: Load
            # self._load(mapped, config)
            #
            # TODO Schritt 5: Erfolg — Datei archivieren
            # self._archive(file_path)

            logger.warning(
                "Pipeline-Schritte sind noch nicht implementiert (siehe TODOs in engine.py)."
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Fehler bei der Verarbeitung von %s: %s", file_path.name, exc)
            self._reject(file_path, [str(exc)])

    # ----- Hilfsmethoden ---------------------------------------------------

    def _archive(self, file_path: Path) -> None:
        """Datei ins Archiv verschieben."""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        target = self.archive_dir / file_path.name
        shutil.move(str(file_path), str(target))
        logger.info("Archiviert: %s", target)

    def _reject(self, file_path: Path, errors: list[str]) -> None:
        """Datei in Reject-Queue verschieben und Fehlerbericht ablegen."""
        self.reject_dir.mkdir(parents=True, exist_ok=True)
        target = self.reject_dir / file_path.name
        shutil.move(str(file_path), str(target))

        error_report = self.reject_dir / (file_path.name + ".errors.txt")
        error_report.write_text("\n".join(errors), encoding="utf-8")
        logger.warning("Rejected: %s (%d Fehler)", target, len(errors))
