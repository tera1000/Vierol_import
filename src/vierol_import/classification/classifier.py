"""
Inhaltsbasierte Klassifikation (Erkennung) fuer headerlose Dateien.

Dateinamen sind bei den externen Quellen KEIN verlaessliches Merkmal
(jede Lieferung heisst anders). Stabil pro Quelle sind dagegen:
Trennzeichen, Spaltenanzahl und die Struktur der Spalteninhalte.

Ablauf pro (Datei, Config)-Paar:

  1. K.O.-Kriterien — wenn eines fehlschlaegt, ist der Score 0.0:
     a) Datei laesst sich mit Encoding + Trennzeichen der Config parsen.
     b) Spaltenanzahl stimmt exakt mit der Config ueberein.

  2. Fein-Score (0.0 .. 1.0):
     Stichprobe der ersten N Zeilen; jede Zelle wird gegen Typ und
     optionales Regex-Muster ihrer Spaltendefinition geprueft.
     Score = passende Zellen / geprueft Zellen.

Das Ergebnis fuer eine Datei ist ein RANKING ueber alle Configs im
Katalog — die Grundlage fuer den Quellen-Vorschlag im interaktiven
CLI-Modus ("Diese Datei sieht zu 96% nach Topmotive aus").
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.encoding_erkennung import erkenne_encoding
from vierol_import.typen import passt_zelle

logger = logging.getLogger(__name__)


# --- Ergebnis-Struktur --------------------------------------------------------


@dataclass
class KlassifikationsErgebnis:
    """Bewertung EINER Config fuer EINE Datei."""

    quelle: str
    score: float
    ko_grund: str | None = None  # gesetzt, wenn ein K.O.-Kriterium gegriffen hat

    @property
    def moeglich(self) -> bool:
        return self.ko_grund is None


@dataclass
class VorschlagsRanking:
    """Alle Bewertungen fuer eine Datei, absteigend nach Score sortiert.

    Interpretation fuer den interaktiven Modus:
      - bester Score >= Schwellenwert der Config -> sicherer Vorschlag
      - mehrere Quellen nah beieinander          -> User entscheidet
      - alle Scores 0                            -> keine Config passt,
        Kandidat fuer den "neue Config anlegen"-Assistenten
    """

    datei: Path
    ergebnisse: list[KlassifikationsErgebnis]

    @property
    def bester(self) -> KlassifikationsErgebnis | None:
        kandidaten = [e for e in self.ergebnisse if e.moeglich]
        return kandidaten[0] if kandidaten else None

    def ist_eindeutig(
        self, schwellenwert: float, mindest_vorsprung: float = 0.10
    ) -> bool:
        """Zuordnung eindeutig, wenn:
          - der beste Kandidat ueber der Schwelle liegt UND
          - er einen deutlichen Vorsprung zum Zweitbesten hat.

        Wird vom Batch-Modus (ZIP-Import, run) genutzt, um zu
        entscheiden, ob eine Datei ohne Rueckfrage durchlaufen darf
        oder ob der User bei Mehrdeutigkeit einbezogen werden muss.
        """
        kandidaten = [e for e in self.ergebnisse if e.moeglich]
        if not kandidaten:
            return False
        if kandidaten[0].score < schwellenwert:
            return False
        # Nur ein Kandidat -> automatisch eindeutig
        if len(kandidaten) < 2:
            return True
        vorsprung = kandidaten[0].score - kandidaten[1].score
        return vorsprung >= mindest_vorsprung

    def ist_eindeutig(self, schwellenwert_fuer_bester: float) -> bool:
        """True wenn genau EINE Quelle ueber ihrer Schwelle liegt.

        Sicherheits-Check fuer den Batch-Modus: Automatik nur wenn kein
        anderer Kandidat auch nahe rankomat. Vermeidet, dass Dateien
        automatisch in die falsche Zieltabelle geschrieben werden, wenn
        zwei Configs strukturell gleich aussehen."""
        kandidaten = [e for e in self.ergebnisse if e.moeglich]
        if not kandidaten:
            return False
        if kandidaten[0].score < schwellenwert_fuer_bester:
            return False
        # Zweiter Kandidat auch ueber (irgend-)Schwellenwert? -> mehrdeutig
        if len(kandidaten) >= 2 and kandidaten[1].score >= schwellenwert_fuer_bester:
            return False
        return True


# --- Kern-Logik ---------------------------------------------------------------


def _lese_stichprobe(
    file_path: Path, trennzeichen: str, encoding: str, max_zeilen: int
) -> list[list[str]] | None:
    """Erste N Zeilen als Spaltenlisten lesen. None bei Parse-Fehlern
    (falsches Encoding, kaputte Datei) — das ist ein K.O., kein Absturz."""
    try:
        with open(file_path, newline="", encoding=encoding) as f:
            reader = csv.reader(f, delimiter=trennzeichen)
            zeilen = []
            for i, zeile in enumerate(reader):
                if i >= max_zeilen:
                    break
                if zeile:  # komplett leere Zeilen ueberspringen
                    zeilen.append(zeile)
            return zeilen
    except (UnicodeDecodeError, csv.Error, OSError) as e:
        logger.debug("Stichprobe aus %s nicht lesbar: %s", file_path.name, e)
        return None


def bewerte_datei(file_path: Path, cfg: QuellenConfig) -> KlassifikationsErgebnis:
    """Eine Datei gegen genau eine Quellen-Config bewerten."""
    d = cfg.datei
    encoding = erkenne_encoding(file_path, wunsch=d.encoding)
    zeilen = _lese_stichprobe(
        file_path, d.trennzeichen, encoding, cfg.klassifikation.stichprobe_zeilen
    )

    # K.O. a): nicht parsebar
    if zeilen is None or not zeilen:
        return KlassifikationsErgebnis(
            quelle=cfg.name, score=0.0, ko_grund="Datei nicht lesbar/leer"
        )

    # Header-Zeile ueberspringen, falls die Quelle einen hat
    if d.hat_header:
        zeilen = zeilen[1:]
        if not zeilen:
            return KlassifikationsErgebnis(
                quelle=cfg.name, score=0.0, ko_grund="Nur Header, keine Datenzeilen"
            )

    # K.O. b): KEINE einzige Zeile hat die erwartete Spaltenanzahl.
    # (Das deutet auf falsches Trennzeichen oder eine andere Quelle hin.
    #  Einzelne abweichende Zeilen sind dagegen nur ein Qualitaets-
    #  problem — sie senken den Score, aber die Quelle bleibt waehlbar,
    #  damit der User zur Validierung mit ihrem Fehlerbericht kommt.)
    erwartet = cfg.spalten_anzahl
    passende_zeilen = [z for z in zeilen if len(z) == erwartet]
    if not passende_zeilen:
        gefunden = len(zeilen[0])
        return KlassifikationsErgebnis(
            quelle=cfg.name,
            score=0.0,
            ko_grund=f"Spaltenanzahl {gefunden} statt {erwartet}",
        )

    # Fein-Score: Zellen gegen Typ + Muster ihrer Spaltendefinition,
    # gewichtet mit dem Anteil strukturell passender Zeilen.
    spalten_sortiert = sorted(cfg.spalten, key=lambda s: s.position)
    geprueft = 0
    treffer = 0
    for zeile in passende_zeilen:
        for spalte in spalten_sortiert:
            geprueft += 1
            if passt_zelle(zeile[spalte.position], spalte):
                treffer += 1

    zellen_score = treffer / geprueft if geprueft else 0.0
    zeilen_anteil = len(passende_zeilen) / len(zeilen)
    score = zellen_score * zeilen_anteil
    return KlassifikationsErgebnis(quelle=cfg.name, score=round(score, 4))


def klassifiziere(
    file_path: Path, configs: dict[str, QuellenConfig]
) -> VorschlagsRanking:
    """Eine Datei gegen ALLE Configs im Katalog bewerten.

    Liefert das vollstaendige Ranking (auch K.O.-Ergebnisse mit Grund),
    damit das CLI dem User transparent zeigen kann, WARUM eine Quelle
    nicht in Frage kommt.
    """
    ergebnisse = [bewerte_datei(file_path, cfg) for cfg in configs.values()]
    ergebnisse.sort(key=lambda e: e.score, reverse=True)

    ranking = VorschlagsRanking(datei=file_path, ergebnisse=ergebnisse)

    if ranking.bester:
        logger.info(
            "Klassifikation %s: bester Kandidat '%s' (Score %.2f)",
            file_path.name,
            ranking.bester.quelle,
            ranking.bester.score,
        )
    else:
        logger.info("Klassifikation %s: keine Config passt.", file_path.name)

    return ranking