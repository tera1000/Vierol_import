"""
Klassifikator: ordnet eine Datei einer Konfiguration aus dem Katalog zu.

HIER STECKT EINER DER KERN-BEITRAEGE DER ARBEIT. Ueberlege dir gut,
wie du klassifizierst:

  Ansatz A — Nur Dateiname-Muster (glob / fnmatch)
     schnell, einfach, aber empfindlich gegen Umbenennungen

  Ansatz B — Nur Header-Signatur (welche Spalten in der CSV?)
     robust, aber ignoriert Kontext-Informationen im Dateinamen

  Ansatz C — Kombiniert (Dateiname + Header, gewichteter Score)  <-- Empfehlung
     robuster, dokumentierbar, in der Thesis gut begruendbar

Vergleiche die Ansaetze im Konzept-Kapitel deiner Thesis.
"""

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from vierol_import.catalog.reader import CatalogReader, Configuration

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    configuration: Configuration | None
    score: float
    reason: str


class Classifier:
    def __init__(self, catalog: CatalogReader, min_score: float = 0.8) -> None:
        self.catalog = catalog
        self.min_score = min_score

    def classify(self, file_path: Path) -> ClassificationResult:
        """
        Beste Konfiguration fuer die Datei ermitteln.

        Gibt eine ClassificationResult mit Konfiguration oder None zurueck.
        Wenn kein Match ueber min_score liegt, ist configuration=None.
        """
        # TODO Header der Datei einlesen (nur die erste Zeile) --
        # z.B. mit pandas.read_csv(nrows=0) oder ganz einfach open() + csv-Modul
        header: list[str] = self._read_header(file_path)

        best: ClassificationResult = ClassificationResult(
            configuration=None, score=0.0, reason="kein Match"
        )

        for cfg in self.catalog.configurations:
            score = self._score(file_path, header, cfg)
            logger.debug("Score fuer %s vs %s: %.2f", file_path.name, cfg.name, score)
            if score > best.score:
                best = ClassificationResult(
                    configuration=cfg, score=score, reason=f"bester Match: {cfg.name}"
                )

        if best.score < self.min_score:
            return ClassificationResult(
                configuration=None,
                score=best.score,
                reason=f"kein Match ueber Schwellwert {self.min_score}",
            )
        return best

    # ----- Interne Bewertungslogik ------------------------------------------

    def _read_header(self, file_path: Path) -> list[str]:
        # TODO robuster machen (Encoding-Erkennung, andere Trennzeichen)
        try:
            df = pd.read_csv(file_path, nrows=0, sep=None, engine="python")
            return list(df.columns)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Header konnte nicht gelesen werden: %s", exc)
            return []

    def _score(
        self, file_path: Path, header: list[str], cfg: Configuration
    ) -> float:
        """
        Kombinierter Score aus Dateiname-Match und Header-Uebereinstimmung.

        TODO: eigene Gewichtung entwickeln und begruenden.
        Beispiel-Skizze (nicht endgueltig):
            - Dateiname matcht Muster: +0.5
            - Anteil der Pflichtspalten, die im Header vorkommen: +0.5 * Anteil
        """
        klass = cfg.klassifikation
        score = 0.0

        # Dateiname
        pattern = klass.get("dateiname_muster")
        if pattern and fnmatch.fnmatch(file_path.name, pattern):
            score += 0.5

        # Pflichtspalten
        required = klass.get("pflicht_spalten", [])
        if required:
            matched = sum(1 for col in required if col in header)
            score += 0.5 * (matched / len(required))

        return score
