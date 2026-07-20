"""
Import-Engine: Orchestriert die vier Verarbeitungsstufen fuer EINE Datei.

  Erkennung -> Validierung -> Mapping -> Load

Warum eine eigene Klasse und keine Funktion in main.py?

  1. Klare Trennung UI ↔ Logik: `main.py` beschreibt WAS auf der
     Konsole passieren soll (User-Interaktion, Farben, Prompts). Die
     Engine beschreibt WIE eine Datei durch die Pipeline geht. Beide
     lassen sich unabhaengig aendern.

  2. Testbarkeit: Die Engine bekommt Configs, DB-Pfad und Zeitstempel
     per Konstruktor injiziert — Tests koennen sie ohne CLI aufrufen.

  3. Zwei Betriebsmodi, eine Engine: Der interaktive `import-file`-
     Befehl UND der Batch-`run`-Befehl verwenden dieselbe Engine. Der
     Unterschied liegt allein darin, WIE die Quelle bestimmt wird
     (Vorschlag+Rueckfrage vs. hoechster Score automatisch).

Die Engine kennt bewusst kein `click` und keine Verzeichnisse. Der
Aufrufer (CLI) uebersetzt das `VerarbeitungsErgebnis` in Konsolen-
ausgabe UND in ein Verschieben von Dateien nach archive/ oder reject/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.classification.classifier import (
    KlassifikationsErgebnis,
    VorschlagsRanking,
    klassifiziere,
)
from vierol_import.loading.loader import PKKonfliktFehler, lade_sqlite
from vierol_import.mapping.mapper import MappingErgebnis, mappe
from vierol_import.validation.validator import ValidierungsFehler, validiere

logger = logging.getLogger(__name__)


class Status(str, Enum):
    """Endzustand einer Datei nach Durchlauf der Pipeline."""

    GELADEN = "geladen"
    ABGELEHNT_UNBEKANNT = "abgelehnt_unbekannt"    # keine Config passt
    ABGELEHNT_UNSICHER = "abgelehnt_unsicher"      # bester Score < Schwelle
    ABGELEHNT_UNGUELTIG = "abgelehnt_ungueltig"    # Validierung fehlgeschlagen
    ABGELEHNT_PK_KONFLIKT = "abgelehnt_pk_konflikt"  # pk_konflikt=reject griff
    ABGELEHNT_LADEFEHLER = "abgelehnt_ladefehler"  # Exception beim Load


@dataclass
class VerarbeitungsErgebnis:
    """Vollstaendiges Resultat eines Datei-Durchlaufs.

    Enthaelt alles, was der Aufrufer fuer Konsolenausgabe, Log und
    Reject-Bericht braucht — die Engine hat keine Seiteneffekte auf
    Dateisystem oder stdout, nur die DB.
    """

    datei: Path
    status: Status
    quelle: str | None = None              # gewaehlte Config, wenn bestimmt
    score: float | None = None             # Erkennungsscore, wenn berechnet
    ranking: VorschlagsRanking | None = None
    zeilen_gesamt: int = 0
    zeilen_geladen: int = 0
    zeilen_uebersprungen: int = 0          # PK existierte bereits (Modus skip)
    fehler_grund: str = ""
    fehler_details: list[str] = field(default_factory=list)
    mapping: MappingErgebnis | None = None  # fuer Vorschau (nur bei Erfolg der ersten Stufen)
    cfg: QuellenConfig | None = None        # Config-Referenz fuer den Load-Aufruf

    @property
    def erfolg(self) -> bool:
        return self.status is Status.GELADEN

    @property
    def bereit_zum_schreiben(self) -> bool:
        """True, wenn Vorbereitung (Validierung + Mapping) erfolgreich
        durchgelaufen ist, aber noch NICHT geschrieben wurde."""
        return self.mapping is not None and self.status is Status.GELADEN


class ImportEngine:
    """Orchestriert Erkennung/Validierung/Mapping/Load fuer eine Datei."""

    def __init__(
        self,
        configs: dict[str, QuellenConfig],
        db_pfad: Path,
        ladezeit: datetime | None = None,
    ) -> None:
        if not configs:
            raise ValueError("Engine braucht mindestens eine Config.")
        self.configs = configs
        self.db_pfad = db_pfad
        self.ladezeit = ladezeit or datetime.now()

    # --- Modus 1: automatisch (Batch) ----------------------------------------

    def verarbeite_auto(self, datei: Path) -> VerarbeitungsErgebnis:
        """Vollautomatisch: beste Quelle waehlen, verarbeiten oder ablehnen.

        Nur ueber dem quellenspezifischen Schwellenwert wird verarbeitet —
        alles darunter ist zu unsicher fuer den Batch-Modus.
        """
        ranking = klassifiziere(datei, self.configs)
        bester = ranking.bester

        if bester is None:
            return VerarbeitungsErgebnis(
                datei=datei,
                status=Status.ABGELEHNT_UNBEKANNT,
                ranking=ranking,
                fehler_grund="Keine Config passt zu dieser Datei",
                fehler_details=[
                    f"{e.quelle}: {e.ko_grund}" for e in ranking.ergebnisse
                ],
            )

        cfg = self.configs[bester.quelle]
        schwelle = cfg.klassifikation.schwellenwert
        if bester.score < schwelle:
            return VerarbeitungsErgebnis(
                datei=datei,
                status=Status.ABGELEHNT_UNSICHER,
                quelle=bester.quelle,
                score=bester.score,
                ranking=ranking,
                fehler_grund=(
                    f"Kein sicherer Vorschlag "
                    f"(bester Score {bester.score:.0%} < Schwellenwert {schwelle:.0%})"
                ),
                fehler_details=[self._ranking_zeile(e) for e in ranking.ergebnisse],
            )

        return self._verarbeite_mit_quelle(datei, cfg, ranking, bester)

    # --- Modus 2: mit vorgegebener Quelle (interaktiv oder --quelle) ---------

    def verarbeite_mit_quelle(
        self, datei: Path, quelle: str
    ) -> VerarbeitungsErgebnis:
        """Mit fest gewaehlter Quelle: Erkennung uebersprungen, direkt validieren."""
        if quelle not in self.configs:
            raise ValueError(
                f"Quelle '{quelle}' nicht im Katalog. Verfuegbar: "
                f"{sorted(self.configs)}"
            )
        return self._verarbeite_mit_quelle(datei, self.configs[quelle], None, None)

    # --- gemeinsamer Kern ----------------------------------------------------

    def _verarbeite_mit_quelle(
        self,
        datei: Path,
        cfg: QuellenConfig,
        ranking: VorschlagsRanking | None,
        bester: KlassifikationsErgebnis | None,
    ) -> VerarbeitungsErgebnis:
        """Vorbereitung: Validierung + Mapping. KEIN Load.

        Der Aufrufer entscheidet danach (mit User-Bestaetigung), ob
        `schreibe()` aufgerufen wird. So kann eine Vorschau des
        MappingErgebnisses angezeigt werden, bevor Daten die
        Zieltabelle beruehren.
        """
        e = VerarbeitungsErgebnis(
            datei=datei,
            status=Status.GELADEN,  # optimistisch — wird bei Fehler ueberschrieben
            quelle=cfg.name,
            score=bester.score if bester else None,
            ranking=ranking,
            cfg=cfg,
        )

        v = validiere(datei, cfg)
        e.zeilen_gesamt = v.zeilen_gesamt
        if not v.ok:
            e.status = Status.ABGELEHNT_UNGUELTIG
            e.fehler_grund = (
                f"{v.zeilen_fehlerhaft} von {v.zeilen_gesamt} Zeilen fehlerhaft"
                + (" (Anzeige nach 50 Fehlern abgebrochen)" if v.abgebrochen else "")
            )
            e.fehler_details = [str(f) for f in v.fehler]
            return e

        try:
            e.mapping = mappe(datei, cfg, ladezeit=self.ladezeit)
        except Exception as ex:
            logger.exception("Mapping-Fehler fuer %s", datei.name)
            e.status = Status.ABGELEHNT_LADEFEHLER
            e.fehler_grund = f"Fehler beim Mapping: {ex}"

        return e

    def schreibe(self, ergebnis: VerarbeitungsErgebnis) -> VerarbeitungsErgebnis:
        """Fuehrt den Load-Schritt aus. Setzt voraus, dass das Ergebnis
        `bereit_zum_schreiben` ist (Validierung + Mapping durchgelaufen).

        Ist gedacht als zweiter Schritt nach `verarbeite_mit_quelle`/
        `verarbeite_auto` — dazwischen kann der Aufrufer eine Vorschau
        anzeigen und eine User-Bestaetigung einholen.
        """
        if not ergebnis.bereit_zum_schreiben:
            raise ValueError(
                "schreibe() nur aufrufen, wenn ergebnis.bereit_zum_schreiben True ist."
            )
        assert ergebnis.mapping is not None and ergebnis.cfg is not None

        try:
            l = lade_sqlite(ergebnis.mapping, ergebnis.cfg, self.db_pfad)
            ergebnis.zeilen_geladen = l.zeilen_geladen
            ergebnis.zeilen_uebersprungen = l.zeilen_uebersprungen
        except PKKonfliktFehler as ex:
            ergebnis.status = Status.ABGELEHNT_PK_KONFLIKT
            ergebnis.fehler_grund = str(ex)
        except Exception as ex:
            logger.exception("Load-Fehler fuer %s", ergebnis.datei.name)
            ergebnis.status = Status.ABGELEHNT_LADEFEHLER
            ergebnis.fehler_grund = f"Fehler beim Laden: {ex}"

        return ergebnis

    def verarbeite_auto_und_schreibe(self, datei: Path) -> VerarbeitungsErgebnis:
        """Batch-Convenience: `verarbeite_auto()` + direkt `schreibe()`.

        Wird vom `run`/`watch`-Modus verwendet — dort ist keine
        User-Bestaetigung vorgesehen, weil unbeaufsichtigt.
        """
        e = self.verarbeite_auto(datei)
        if e.bereit_zum_schreiben:
            self.schreibe(e)
        return e

    @staticmethod
    def _ranking_zeile(e: KlassifikationsErgebnis) -> str:
        if e.moeglich:
            return f"{e.quelle}: Score {e.score:.0%}"
        return f"{e.quelle}: K.O. — {e.ko_grund}"