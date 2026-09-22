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

--- quarantaene_zeilen: strukturierte Objekte, keine Tupel ---
Ein fruehere Version dieser Datei baute quarantaene_zeilen ueber eine
eigene _quarantaene_zeilen_lesen()-Methode, die die Datei ein zweites
Mal selbst einlas und Tupel (nr, zeile_liste, grund_string) erzeugte.
Das ging an den strukturierten ValidierungsFehler-Objekten aus dem
Validator vorbei (die bereits spalte/wert/roh_zeile als echte
Attribute tragen) und fuehrte dazu, dass audit_log._fehler_eintraege_
bauen ueber den Tupel-Fallback lief: spalte/wert/roh_zeile blieben
NULL, waehrend grund den zusammengeklebten "spalte: text"-String
bekam. Fix: die ValidierungsFehler-Objekte aus dem Validator werden
jetzt DIREKT in quarantaene_zeilen uebernommen (list(v.fehler)) —
keine Rekonstruktion, kein Zweit-Read der Datei.
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
from vierol_import.loading.loader import PKKonfliktFehler, lade
from vierol_import.mapping.mapper import MappingErgebnis, mappe
from vierol_import.monitoring.audit_log import logge_lauf
from vierol_import.validation.validator import validiere

logger = logging.getLogger(__name__)


class Status(str, Enum):
    """Endzustand einer Datei nach Durchlauf der Pipeline.

    Vier Grund-Kategorien laut Briefing:
      - erfolgreich (=geladen)
      - klaerung (Klassifikation unklar oder unbekannt)
      - abgelehnt (Validierung/Ladefehler)
      - konflikt (PK-Konflikt bei strategie=append/reject)

    Die detaillierten Werte unten sind Subtypen fuer bessere Diagnose,
    lassen sich aber ueber ihr Prefix in die vier Briefing-Kategorien
    zusammenfassen.
    """

    GELADEN = "geladen"
    KLAERUNG_UNBEKANNT = "klaerung_unbekannt"      # keine Config passt
    KLAERUNG_UNSICHER = "klaerung_unsicher"        # bester Score < Schwelle
    ABGELEHNT_UNGUELTIG = "abgelehnt_ungueltig"    # Validierung fehlgeschlagen
    KONFLIKT_PK = "konflikt_pk"                    # pk_konflikt=reject griff
    ABGELEHNT_LADEFEHLER = "abgelehnt_ladefehler"  # Exception beim Load

    # Rueckwaerts-kompatible Aliase (alte Werte, damit alte Log-Eintraege
    # nicht ins Leere zeigen).
    ABGELEHNT_UNBEKANNT = "abgelehnt_unbekannt"
    ABGELEHNT_UNSICHER = "abgelehnt_unsicher"
    ABGELEHNT_PK_KONFLIKT = "abgelehnt_pk_konflikt"


@dataclass
class VerarbeitungsErgebnis:
    """Vollstaendiges Resultat eines Datei-Durchlaufs.

    Enthaelt alles, was der Aufrufer fuer Konsolenausgabe, Log und
    Reject-Bericht braucht — die Engine hat keine Seiteneffekte auf
    Dateisystem oder stdout, nur die DB.
    """

    datei: Path
    status: Status
    quelle: str | None = None
    score: float | None = None
    ranking: VorschlagsRanking | None = None
    zeilen_gesamt: int = 0
    zeilen_geladen: int = 0
    zeilen_uebersprungen: int = 0
    zeilen_quarantaene: int = 0            # Zeilen, die im partiellen Modus abgelehnt wurden
    fehler_grund: str = ""
    fehler_details: list[str] = field(default_factory=list)
    # Strukturierte Fehler-Objekte (ValidierungsFehler oder
    # OracleBatchFehler) — NICHT Tupel. Beide Typen haben die
    # Attribute zeile/spalte/wert/roh_zeile/grund und werden von
    # audit_log._fehler_eintraege_bauen per Duck-Typing gelesen.
    quarantaene_zeilen: list = field(default_factory=list)
    mapping: MappingErgebnis | None = None
    cfg: QuellenConfig | None = None

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
        verbindungen: dict,
        ladezeit: datetime | None = None,
        benutzer_modus: str = "cli",
        passwort_provider=None,
    ) -> None:
        if not configs:
            raise ValueError("Engine braucht mindestens eine Config.")
        if not verbindungen:
            raise ValueError(
                "Engine braucht mindestens eine Verbindung "
                "(zielsysteme/*.yaml)."
            )
        self.configs = configs
        self.verbindungen = verbindungen
        self.db_pfad = self._audit_db_pfad()
        self.ladezeit = ladezeit or datetime.now()
        self.benutzer_modus = benutzer_modus
        # Callback fuer Oracle-Passwort. Signatur: (verbindungsname) -> str.
        # GUI: liest aus Session-State (oder oeffnet Dialog).
        # CLI: getpass.getpass()-Prompt.
        # Bei None wird Oracle nicht funktionieren.
        self.passwort_provider = passwort_provider

    def _audit_db_pfad(self) -> Path:
        """Waehle eine SQLite-Datei fuer das Audit-Log.

        Auch wenn wir produktiv nach Oracle schreiben, bleibt der Audit-
        Log lokal — er soll immer verfuegbar sein, unabhaengig von der
        Erreichbarkeit einer Zielserver-DB.
        """
        for v in self.verbindungen.values():
            if v.typ == "sqlite" and v.pfad:
                return Path(v.pfad)
        # Fallback, falls es gar keine SQLite-Verbindung gibt
        return Path("data/vierol_import.sqlite")

    # --- Modus 1: automatisch (Batch) ----------------------------------------

    def verarbeite_auto(self, datei: Path) -> VerarbeitungsErgebnis:
        """Vollautomatisch: beste Quelle waehlen, verarbeiten oder ablehnen.

        Nur ueber dem quellenspezifischen Schwellenwert wird verarbeitet —
        alles darunter ist zu unsicher fuer den Batch-Modus.
        """
        ranking = klassifiziere(datei, self.configs)
        bester = ranking.bester

        if bester is None:
            e = VerarbeitungsErgebnis(
                datei=datei,
                status=Status.KLAERUNG_UNBEKANNT,
                ranking=ranking,
                fehler_grund="Keine Config passt zu dieser Datei",
                fehler_details=[
                    f"{e.quelle}: {e.ko_grund}" for e in ranking.ergebnisse
                ],
            )
            self._loggen(e)
            return e

        cfg = self.configs[bester.quelle]
        schwelle = cfg.klassifikation.schwellenwert
        if bester.score < schwelle:
            e = VerarbeitungsErgebnis(
                datei=datei,
                status=Status.KLAERUNG_UNSICHER,
                quelle=bester.quelle,
                score=bester.score,
                ranking=ranking,
                fehler_grund=(
                    f"Kein sicherer Vorschlag "
                    f"(bester Score {bester.score:.0%} < Schwellenwert {schwelle:.0%})"
                ),
                fehler_details=[self._ranking_zeile(e) for e in ranking.ergebnisse],
            )
            self._loggen(e)
            return e

        # NEU: Mehrdeutigkeit pruefen — wenn ein zweiter Kandidat ebenfalls
        # ueber (irgend-)einem Schwellenwert liegt, ist das keine sichere
        # Zuordnung. Wir raten dann NICHT, sondern lehnen ab (Batch-Modus)
        # bzw. lassen die interaktive Nachfrage in verarbeite_auto_und_schreibe
        # entscheiden. Der User bekommt sofort die konkurrierenden Configs
        # als Fehlerdetails zu sehen.
        moegliche = [e for e in ranking.ergebnisse if e.moeglich]
        wenn_zwei_ueber_schwelle = (
            len(moegliche) >= 2
            and moegliche[1].score >= self.configs[moegliche[1].quelle]
                                          .klassifikation.schwellenwert
        )
        if wenn_zwei_ueber_schwelle:
            e = VerarbeitungsErgebnis(
                datei=datei,
                status=Status.KLAERUNG_UNSICHER,
                quelle=bester.quelle,
                score=bester.score,
                ranking=ranking,
                fehler_grund=(
                    f"Mehrdeutige Zuordnung — {len(moegliche)} Configs "
                    f"passen ueber ihrer Schwelle. Bitte manuell entscheiden "
                    f"(--quelle <name>)."
                ),
                fehler_details=[self._ranking_zeile(e) for e in moegliche],
            )
            self._loggen(e)
            return e

        e = self._verarbeite_mit_quelle(datei, cfg, ranking, bester)
        if not e.bereit_zum_schreiben:
            self._loggen(e)
        return e

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
        e = self._verarbeite_mit_quelle(datei, self.configs[quelle], None, None)
        # Bei Ablehnung sofort loggen (schreibe() wird nicht mehr aufgerufen).
        # Bei bereit_zum_schreiben: NICHT loggen — das macht schreibe() spaeter.
        if not e.bereit_zum_schreiben:
            self._loggen(e)
        return e

    # --- gemeinsamer Kern ----------------------------------------------------

    def _verarbeite_mit_quelle(
        self,
        datei: Path,
        cfg: QuellenConfig,
        ranking: VorschlagsRanking | None,
        bester: KlassifikationsErgebnis | None,
    ) -> VerarbeitungsErgebnis:
        """Vorbereitung: Validierung + Mapping. KEIN Load.

        Fehlertoleranz wird durch `fehler_schwelle` (0.0-1.0) oder
        `fehler_modus` gesteuert; `fehler_schwelle` hat Vorrang.

        Verhalten:
          - Fehlerquote UEBER Schwelle -> ganze Datei abgelehnt
          - Fehlerquote UNTER/GLEICH Schwelle -> gute Zeilen weiter,
            kaputte Zeilen fuer die Quarantaene-CSV sammeln
        """
        schwelle = self._effektive_fehler_schwelle(cfg)
        # partiell=True zwingt den Validator, ALLE Zeilen zu pruefen
        # (statt bei MAX_FEHLER abzubrechen). Nur dann kennen wir die
        # echte Quote und koennen sie gegen die Schwelle vergleichen.
        alles_oder_nichts = (schwelle == 0.0)

        e = VerarbeitungsErgebnis(
            datei=datei,
            status=Status.GELADEN,
            quelle=cfg.name,
            score=bester.score if bester else None,
            ranking=ranking,
            cfg=cfg,
        )

        v = validiere(datei, cfg, partiell=not alles_oder_nichts)
        e.zeilen_gesamt = v.zeilen_gesamt

        if alles_oder_nichts:
            # Klassisch: ein Fehler -> alles ablehnen
            if not v.ok:
                e.status = Status.ABGELEHNT_UNGUELTIG
                e.fehler_grund = (
                    f"{v.zeilen_fehlerhaft} von {v.zeilen_gesamt} Zeilen fehlerhaft"
                    + (" (Anzeige nach 50 Fehlern abgebrochen)" if v.abgebrochen else "")
                )
                e.fehler_details = [str(f) for f in v.fehler]
                # Strukturierte Fehler-Objekte direkt weitergeben — sie
                # tragen bereits spalte/wert/roh_zeile (aus validator.py
                # gesetzt). So funktioniert die vollstaendige Fehler-
                # Diagnose (inkl. roh_zeile) auch im alles-oder-nichts-
                # Modus, nicht nur bei aktivierter fehler_schwelle.
                e.quarantaene_zeilen = list(v.fehler)
                return e
            gute_zeilen = None
        else:
            # Partielle Toleranz: Quote pruefen
            if v.zeilen_gesamt > 0:
                quote = v.zeilen_fehlerhaft / v.zeilen_gesamt
            else:
                quote = 0.0

            if quote > schwelle:
                # Ueber der Schwelle: Datei komplett ablehnen (Log-Status
                # 'abgelehnt' analog zum Briefing).
                e.status = Status.ABGELEHNT_UNGUELTIG
                e.fehler_grund = (
                    f"Fehlerquote {quote:.1%} ueber Schwelle {schwelle:.1%} "
                    f"({v.zeilen_fehlerhaft} von {v.zeilen_gesamt} Zeilen fehlerhaft)"
                )
                e.fehler_details = [str(f) for f in v.fehler]
                e.quarantaene_zeilen = list(v.fehler)
                return e

            # Unter Schwelle: gute Zeilen mappen, kaputte in Quarantaene.
            # WICHTIG: die ValidierungsFehler-Objekte aus dem Validator
            # werden DIREKT weitergegeben (nicht neu aus der Datei
            # rekonstruiert) — sie tragen bereits spalte, wert und
            # roh_zeile als echte Attribute. Der Audit-Log-Writer liest
            # diese per Duck-Typing (siehe audit_log.py).
            e.zeilen_quarantaene = v.zeilen_fehlerhaft
            e.fehler_details = [str(f) for f in v.fehler]
            e.quarantaene_zeilen = list(v.fehler)
            gute_zeilen = v.gute_zeilen

            if not v.gute_zeilen:
                e.status = Status.ABGELEHNT_UNGUELTIG
                e.fehler_grund = (
                    f"Alle {v.zeilen_gesamt} Zeilen fehlerhaft "
                    "(unter Schwelle, aber nichts zu laden)"
                )
                return e

        try:
            e.mapping = mappe(datei, cfg, ladezeit=self.ladezeit, nur_zeilen=gute_zeilen)
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
            verbindung = self._hole_verbindung(ergebnis.cfg)
            # Fuer Oracle: Passwort vom Provider holen (GUI-Session-State
            # oder CLI-Prompt). Fuer SQLite bleibt es None (nicht gebraucht).
            passwort = None
            if verbindung.typ == "oracle":
                if self.passwort_provider is None:
                    raise RuntimeError(
                        f"Oracle-Verbindung '{verbindung.name}' braucht ein "
                        "Passwort, aber die Engine hat keinen "
                        "passwort_provider bekommen."
                    )
                passwort = self.passwort_provider(verbindung.name)
            l = lade(ergebnis.mapping, ergebnis.cfg, verbindung,
                     oracle_passwort=passwort)
            ergebnis.zeilen_geladen = l.zeilen_geladen
            ergebnis.zeilen_uebersprungen = l.zeilen_uebersprungen

            # Oracle-spezifisch: strukturierte Batch-Fehler abholen und
            # in die Quarantaene uebertragen. So landen sie ueber
            # _loggen() -> logge_lauf() -> _fehler_eintraege_bauen (Duck-
            # Typing) genauso in import_fehler wie Validierungs-Fehler.
            # Fuer SQLite ist die Funktion nicht relevant und wir
            # ueberspringen den Import.
            if verbindung.typ == "oracle":
                try:
                    from vierol_import.loading.oracle_loader import (
                        hole_letzte_batch_fehler,
                    )
                    batch_fehler = hole_letzte_batch_fehler()
                    if batch_fehler:
                        # An bestehende Quarantaene anhaengen (die kann
                        # bereits Validierungs-Fehler enthalten).
                        ergebnis.quarantaene_zeilen.extend(batch_fehler)
                        ergebnis.zeilen_quarantaene += len(batch_fehler)
                        # fehler_details ergaenzen fuer die Konsole
                        ergebnis.fehler_details.extend(str(bf) for bf in batch_fehler)
                except ImportError:
                    # Sollte nicht passieren, aber sicherheitshalber
                    logger.debug(
                        "Konnte hole_letzte_batch_fehler nicht importieren.",
                    )
        except PKKonfliktFehler as ex:
            ergebnis.status = Status.KONFLIKT_PK
            ergebnis.fehler_grund = str(ex)
        except Exception as ex:
            logger.exception("Load-Fehler fuer %s", ergebnis.datei.name)
            ergebnis.status = Status.ABGELEHNT_LADEFEHLER
            ergebnis.fehler_grund = f"Fehler beim Laden: {ex}"

        # Audit-Log-Eintrag fuer diese Datei schreiben (Erfolg oder Fehler)
        self._loggen(ergebnis)
        return ergebnis

    def _effektive_fehler_schwelle(self, cfg: QuellenConfig) -> float:
        """Ermittelt die effektive Fehlerquoten-Schwelle (0.0 - 1.0).

        Regelung:
          1. Wenn `fehler_schwelle` explizit gesetzt ist -> dieser Wert
          2. Sonst aus `fehler_modus` ableiten:
             - "alles_oder_nichts" -> 0.0 (keine Fehler tolerieren)
             - "partiell" -> 1.0 (alle Fehler tolerieren)
        """
        if cfg.zielsystem.fehler_schwelle is not None:
            return cfg.zielsystem.fehler_schwelle
        if cfg.zielsystem.fehler_modus == "partiell":
            return 1.0
        return 0.0

    def _hole_verbindung(self, cfg: QuellenConfig):
        """Verbindung aus der Registry holen. Wirft ValueError mit klarer
        Fehlermeldung, wenn die Referenz nicht existiert."""
        ref = cfg.zielsystem.ref
        if ref not in self.verbindungen:
            raise ValueError(
                f"Config '{cfg.name}' verweist auf Zielsystem '{ref}', "
                f"das nicht in der Verbindungs-Registry ist. Verfuegbar: "
                f"{sorted(self.verbindungen)}"
            )
        return self.verbindungen[ref]

    def _loggen(self, ergebnis: VerarbeitungsErgebnis) -> None:
        """Ein Ergebnis in die Audit-Tabelle schreiben.

        Wird bei jedem definitiven End-Zustand aufgerufen (Erfolg oder
        Ablehnung). Fehler beim Schreiben ins Audit-Log dürfen nie den
        Import selbst kaputt machen — das behandelt logge_lauf intern.

        Detaillierte Fehler (z. B. die 18 kaputten Zeilen in einer
        ansonsten erfolgreichen 151k-Zeilen-Datei) werden in die
        Tabelle `import_fehler` geschrieben, verknuepft ueber lauf_id.
        """
        logge_lauf(
            self.db_pfad,
            dateiname=ergebnis.datei.name,
            status=ergebnis.status.value,
            quelle=ergebnis.quelle,
            score=ergebnis.score,
            zeilen_gesamt=ergebnis.zeilen_gesamt,
            zeilen_geladen=ergebnis.zeilen_geladen,
            zeilen_uebersprungen=ergebnis.zeilen_uebersprungen,
            zeilen_quarantaene=ergebnis.zeilen_quarantaene,
            fehler_grund=ergebnis.fehler_grund,
            fehler_details=ergebnis.fehler_details,
            quarantaene_zeilen=ergebnis.quarantaene_zeilen,
            benutzer_modus=self.benutzer_modus,
        )

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