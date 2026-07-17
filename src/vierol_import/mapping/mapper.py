"""
Mapping: eine Rohzeile aus der Datei -> ein kanonischer Datensatz
(Dictionary aus Zielfeldname -> Python-Wert).

Zwei Arten von Zielfeldern:

  1. Aus einer Spalte:  quelle="preis" -> ziel="preis"
     Wert wird ueber `typen.konvertiere()` in einen Python-Wert
     ueberfuehrt (str -> float / date / int / bool).

  2. Abgeleitet:        ziel="jahrmonat", funktion="ladezeitpunkt_jahrmonat"
     Wert kommt NICHT aus der Datei, sondern wird pro Import berechnet.
     Die Funktionen leben in _ABLEITUNGEN und werden pro Lauf einmal
     ausgewertet — z. B. bekommen alle Zeilen eines Laufs dasselbe
     `jahrmonat`, was dem Fachkonzept "Ladezeitpunkt der Lieferung"
     entspricht.

Der Mapper wirft keine Exceptions bei Datenfehlern — die Datei ist
vorher durch die Validierung gegangen, alle Zellen sind hier bereits
als typkonform bekannt. Sollte doch etwas schiefgehen, ist das ein
Bug im Meta-Schema (Typ passt nicht zur Realitaet) und darf ruhig laut
scheitern.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.typen import konvertiere

logger = logging.getLogger(__name__)


# --- Abgeleitete Felder ------------------------------------------------------
# Registry: Funktions-Key aus der YAML -> Python-Funktion, die einen
# Wert liefert. Neue Ableitungen werden hier eingetragen und sind ab
# sofort in allen Configs per YAML nutzbar.


def _jahrmonat(ladezeit: datetime) -> str:
    return ladezeit.strftime("%Y%m")


def _datum(ladezeit: datetime) -> Any:
    return ladezeit.date()


def _dateiname(pfad: Path) -> str:
    return pfad.name


_ABLEITUNGEN: dict[str, Callable[[Path, datetime], Any]] = {
    "ladezeitpunkt_jahrmonat": lambda pfad, jetzt: _jahrmonat(jetzt),
    "ladezeitpunkt_datum": lambda pfad, jetzt: _datum(jetzt),
    "dateiname": lambda pfad, jetzt: _dateiname(pfad),
}


# --- Ergebnis-Struktur --------------------------------------------------------


@dataclass
class MappingErgebnis:
    quelle: str                  # Quellen-Name (aus der Config)
    zielfelder: list[str]        # Reihenfolge der Zielfelder (fuer Load)
    saetze: list[dict[str, Any]] # gemappte Datensaetze


# --- Kern-Logik ---------------------------------------------------------------


def mappe(
    file_path: Path,
    cfg: QuellenConfig,
    ladezeit: datetime | None = None,
) -> MappingErgebnis:
    """Ganze Datei mappen und alle Datensaetze zurueckliefern.

    `ladezeit` erlaubt Tests mit fixierter Zeit; im Normalbetrieb wird
    `datetime.now()` verwendet. Wichtig: EIN Zeitstempel pro Lauf, damit
    alle Zeilen dasselbe `jahrmonat` bekommen.
    """
    if ladezeit is None:
        ladezeit = datetime.now()

    saetze = list(_mappe_iter(file_path, cfg, ladezeit))

    # Zielfeld-Reihenfolge: erst Spalten-Regeln (in Reihenfolge der Config),
    # dann abgeleitete Felder. Deterministisch, damit die Load-Stufe die
    # Reihenfolge fuer INSERT-Statements verlaesslich kennt.
    zielfelder = [r.ziel for r in cfg.mapping.regeln] + [
        af.ziel for af in cfg.mapping.abgeleitete_felder
    ]

    logger.info(
        "Mapping %s -> %d Datensaetze, %d Zielfelder",
        file_path.name,
        len(saetze),
        len(zielfelder),
    )
    return MappingErgebnis(quelle=cfg.name, zielfelder=zielfelder, saetze=saetze)


def _mappe_iter(
    file_path: Path, cfg: QuellenConfig, ladezeit: datetime
) -> Iterator[dict[str, Any]]:
    d = cfg.datei
    spalten_nach_name = {s.name: s for s in cfg.spalten}

    # Abgeleitete Werte einmal pro Lauf berechnen
    abgeleitete_werte: dict[str, Any] = {}
    for af in cfg.mapping.abgeleitete_felder:
        funktion = _ABLEITUNGEN.get(af.funktion)
        if funktion is None:
            raise ValueError(
                f"Unbekannte Ableitungs-Funktion '{af.funktion}' in Config "
                f"'{cfg.name}'. Erlaubt: {sorted(_ABLEITUNGEN)}"
            )
        abgeleitete_werte[af.ziel] = funktion(file_path, ladezeit)

    with open(file_path, newline="", encoding=d.encoding) as f:
        reader = csv.reader(f, delimiter=d.trennzeichen)
        if d.hat_header:
            next(reader, None)

        for zeile in reader:
            if not zeile:
                continue

            satz: dict[str, Any] = {}

            for regel in cfg.mapping.regeln:
                spalte = spalten_nach_name[regel.quelle]
                rohwert = zeile[spalte.position]
                satz[regel.ziel] = konvertiere(rohwert, spalte.typ)

            satz.update(abgeleitete_werte)
            yield satz