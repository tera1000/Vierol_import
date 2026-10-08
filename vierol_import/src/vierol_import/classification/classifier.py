"""
Inhaltsbasierte Klassifikation (Erkennung) fuer headerlose Dateien.

Dateinamen sind bei den externen Quellen KEIN verlaessliches Merkmal
(jede Lieferung heisst anders). Stabil pro Quelle sind dagegen:
Trennzeichen, Spaltenanzahl und die Struktur der Spalteninhalte.

Ablauf pro (Datei, Config)-Paar:

  1. K.O.-Kriterien — wenn eines fehlschlaegt, ist der Score 0.0:
     a) Datei laesst sich mit Format, Encoding + Trennzeichen der Config
        parsen.
     b) Spaltenanzahl stimmt exakt mit der Config ueberein.

  2. Fein-Score (0.0 .. 1.0):
     Stichprobe der ersten N Zeilen; jede Zelle wird gegen Typ und
     optionales Regex-Muster ihrer Spaltendefinition geprueft.
     Score = passende Zellen / geprueft Zellen.

Das Ergebnis fuer eine Datei ist ein RANKING ueber alle Configs im
Katalog — die Grundlage fuer den Quellen-Vorschlag im interaktiven
CLI-Modus ("Diese Datei sieht zu 96% nach Topmotive aus").

--- Format-Abstraktion ---
Die Stichprobe wird ueber den zentralen Format-Reader
(catalog.datei_reader.zeilen_iter) bezogen, NICHT ueber einen direkten
csv.reader-Aufruf. Damit nutzt die Klassifikation dieselbe
Format-Abstraktion wie Validierung und Mapping und funktioniert
einheitlich fuer CSV/TXT sowie fuer Excel/XML/JSON. Der Reader
kuemmert sich intern auch um die Header-Behandlung (bei hat_header
startet die erste Datenzeile hinter der Kopfzeile), sodass hier keine
manuelle Header-Sonderbehandlung mehr noetig ist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from vierol_import.catalog.datei_reader import zeilen_iter
from vierol_import.catalog.meta_schema import QuellenConfig
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


# --- Kern-Logik ---------------------------------------------------------------


def _lese_stichprobe(
    file_path: Path, cfg: QuellenConfig, max_zeilen: int
) -> list[list[str]] | None:
    """Erste N Datenzeilen als Spaltenlisten lesen, ueber den zentralen
    Format-Reader.

    Rueckgabe None bei Lese-/Parse-Fehlern (falsches Encoding, Format
    passt nicht zur getesteten Config, kaputte Datei) — das ist ein
    K.O.-Kriterium, kein Absturz. So faellt z.B. eine .xlsx-Datei, die
    gegen eine CSV-Config getestet wird, kontrolliert heraus, statt die
    Klassifikation abzubrechen.

    Der Reader ueberspringt die Kopfzeile bereits selbst (bei
    cfg.datei.hat_header), daher enthaelt die zurueckgegebene Liste
    ausschliesslich Datenzeilen — eine separate Header-Behandlung ist
    hier nicht mehr noetig.
    """
    try:
        zeilen: list[list[str]] = []
        for i, (_nr, felder) in enumerate(zeilen_iter(file_path, cfg.datei)):
            if i >= max_zeilen:
                break
            if felder:  # komplett leere Zeilen ueberspringen
                zeilen.append(felder)
        return zeilen
    except Exception as e:
        # Bewusst breit: der Reader kann je nach Format sehr
        # unterschiedliche Fehler werfen (UnicodeDecodeError bei falschem
        # Encoding, pandas-/lxml-Fehler bei formatfremden Dateien etc.).
        # Fuer die Klassifikation ist jeder davon gleichbedeutend mit
        # "diese Config passt nicht" — also ein K.O., kein Absturz.
        logger.debug("Stichprobe aus %s nicht lesbar: %s", file_path.name, e)
        return None


def bewerte_datei(file_path: Path, cfg: QuellenConfig) -> KlassifikationsErgebnis:
    """Eine Datei gegen genau eine Quellen-Config bewerten."""
    zeilen = _lese_stichprobe(
        file_path, cfg, cfg.klassifikation.stichprobe_zeilen
    )

    # K.O. a): nicht parsebar (Format/Encoding passt nicht, leer, kaputt)
    if zeilen is None or not zeilen:
        return KlassifikationsErgebnis(
            quelle=cfg.name, score=0.0, ko_grund="Datei nicht lesbar/leer"
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