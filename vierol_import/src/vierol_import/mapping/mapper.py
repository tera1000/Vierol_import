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

--- roh_zeilen ---
Zusaetzlich zum gemappten Ergebnis fuehrt der Mapper eine parallele
Liste `roh_zeilen` mit: pro gemapptem Satz die ORIGINAL-Datei-Zeile
(rekonstruiert per `trennzeichen.join(zeile_felder)`). Diese Liste wird
im Load-Fehlerfall vom Oracle-Loader ausgelesen und in
`import_fehler.roh_zeile` gespeichert — damit ist im Fehler-Log exakt
zu sehen was in der Datei stand (z.B. fuer Reklamationen).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.catalog.datei_reader import zeilen_iter
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
    # Parallele Liste zu 'saetze': pro Index die Original-Datei-Zeile
    # (rekonstruiert per trennzeichen.join). Wird im Fehlerfall vom
    # Oracle-Loader in import_fehler.roh_zeile geschrieben.
    roh_zeilen: list[str] = field(default_factory=list)


# --- Kern-Logik ---------------------------------------------------------------


def mappe(
    file_path: Path,
    cfg: QuellenConfig,
    ladezeit: datetime | None = None,
    nur_zeilen: set[int] | None = None,
) -> MappingErgebnis:
    """Ganze Datei mappen und alle Datensaetze zurueckliefern.

    `ladezeit` erlaubt Tests mit fixierter Zeit; im Normalbetrieb wird
    `datetime.now()` verwendet. Wichtig: EIN Zeitstempel pro Lauf, damit
    alle Zeilen dasselbe `jahrmonat` bekommen.

    `nur_zeilen` (optional): fuer den partiellen Modus — nur die
    angegebenen Zeilennummern werden gemappt (die Validierung hat
    entschieden, welche Zeilen sauber sind).
    """
    if ladezeit is None:
        ladezeit = datetime.now()

    # Neuer Weg (Briefing-konform): felder-Liste mit Kategorien
    if cfg.mapping.felder:
        paare = list(_mappe_iter_felder(file_path, cfg, ladezeit, nur_zeilen))
        # 'auto'-Felder werden NICHT ins Ergebnis geschrieben — sie
        # kommen aus der DB (Auto-Increment etc.). Loader ignoriert sie.
        zielfelder = [
            feld.ziel for feld in cfg.mapping.felder
            if feld.kategorie != "auto"
        ]
    else:
        # Alter Weg (rueckwaertskompatibel): regeln + abgeleitete_felder
        paare = list(_mappe_iter(file_path, cfg, ladezeit, nur_zeilen))
        zielfelder = [r.ziel for r in cfg.mapping.regeln] + [
            af.ziel for af in cfg.mapping.abgeleitete_felder
        ]

    # Paare aufteilen in Saetze und roh_zeilen — sie werden parallel
    # gehalten (gleicher Index gehoert zusammen).
    saetze = [p[0] for p in paare]
    roh_zeilen = [p[1] for p in paare]

    logger.info(
        "Mapping %s -> %d Datensaetze, %d Zielfelder",
        file_path.name,
        len(saetze),
        len(zielfelder),
    )
    return MappingErgebnis(
        quelle=cfg.name,
        zielfelder=zielfelder,
        saetze=saetze,
        roh_zeilen=roh_zeilen,
    )


def _mappe_iter_felder(
    file_path: Path, cfg: QuellenConfig, ladezeit: datetime,
    nur_zeilen: set[int] | None = None,
) -> Iterator[tuple[dict[str, Any], str]]:
    """Mapping mit der neuen felder-Struktur (vier Kategorien).

    - quelle: Wert direkt aus Datei
    - auto: NICHT ins Ergebnis (DB setzt Wert)
    - system: Wert aus Kontext (Ladezeitpunkt, Dateiname)
    - berechnet: Wert aus anderen Feldern per Formel

    Reihenfolge der Auswertung:
      1. quelle-Felder aus Zeile lesen + konvertieren
      2. system-Felder aus Kontext befuellen
      3. berechnet-Felder ausrechnen (koennen auf 1+2 zugreifen)
      4. auto-Felder komplett auslassen

    Yield: (satz, roh_zeile) — die roh_zeile ist die Original-Datei-Zeile
    (rekonstruiert per trennzeichen.join der geparsedten Felder), wird
    parallel zum satz durch den Loader gefuehrt.
    """
    d = cfg.datei
    spalten_nach_name = {s.name: s for s in cfg.spalten}

    # System-Werte einmal pro Lauf berechnen (unabhaengig von Zeile)
    system_werte: dict[str, Any] = {}
    for feld in cfg.mapping.felder:
        if feld.kategorie != "system":
            continue
        system_werte[feld.ziel] = _system_wert(feld, file_path, ladezeit)

    encoding = None  # nicht mehr noetig, reader.zeilen_iter regelt das
    for nr, zeile in zeilen_iter(file_path, d):
        if not zeile:
            continue
        if nur_zeilen is not None and nr not in nur_zeilen:
            continue

        # Original-Datei-Zeile rekonstruieren (fuer Fehler-Diagnose).
        # Der Reader hat die Zeile bereits gesplittet — wir joinen sie
        # mit dem Trennzeichen zurueck. Bei CSV/TXT-Dateien mit
        # pipe-Separator ergibt das genau die Original-Zeile. Bei
        # Excel/XML/JSON ist es eine synthetische Darstellung.
        roh_zeile = d.trennzeichen.join(zeile)

        satz: dict[str, Any] = {}

        # 1. quelle-Felder
        for feld in cfg.mapping.felder:
            if feld.kategorie != "quelle":
                continue
            spalte = spalten_nach_name[feld.von]
            rohwert = zeile[spalte.position]
            satz[feld.ziel] = konvertiere(rohwert, spalte.typ)

        # 2. system-Felder
        satz.update(system_werte)

        # 3. berechnet-Felder (koennen auf quelle+system zugreifen)
        for feld in cfg.mapping.felder:
            if feld.kategorie != "berechnet":
                continue
            try:
                satz[feld.ziel] = _auswerte_formel(feld.formel, satz)
            except Exception as e:
                logger.warning(
                    "Berechnetes Feld '%s' in Zeile %d fehlgeschlagen: %s",
                    feld.ziel, nr, e,
                )
                satz[feld.ziel] = None

        # 4. auto-Felder werden NICHT gesetzt (DB uebernimmt)
        yield satz, roh_zeile


def _system_wert(feld, file_path: Path, ladezeit: datetime) -> Any:
    """Ein System-Wert aus dem Verarbeitungskontext.

    Ergaenzt um zwei Pfad-basierte Quellen:
      - elternordner: Name des unmittelbaren Elternordners.
        Beispiel: '.../2026-04-01/Ford_USA/daten.csv' -> 'Ford_USA'
      - pfad_regex: extrahiert einen Wert per Regex aus dem vollen Pfad.
        Der Pfad wird als POSIX-String (Forward-Slashes) uebergeben,
        damit die gleichen Regex-Muster auf Windows und Linux funktionieren.
    """
    quelle = feld.system_quelle

    if quelle == "ladezeitpunkt":
        return ladezeit
    if quelle == "ladezeitpunkt_jahrmonat":
        return ladezeit.strftime("%Y%m")
    if quelle == "ladezeitpunkt_datum":
        return ladezeit.strftime("%Y-%m-%d")
    if quelle == "dateiname":
        return file_path.name

    if quelle == "elternordner":
        # Robuster Zugriff — wenn die Datei im Root liegt, gibt es keinen
        # Elternordner. Wir liefern dann None statt zu crashen.
        parent = file_path.parent
        if str(parent) in (".", "/", ""):
            logger.warning(
                "elternordner angefordert, aber Datei '%s' hat keinen "
                "sinnvollen Elternordner.", file_path,
            )
            return None
        return parent.name

    if quelle == "pfad_regex":
        import re as _re
        # POSIX-Pfad benutzen, damit die gleichen Regex-Muster auf
        # Windows und Linux funktionieren (Backslashes waeren sonst ein
        # Regex-Falltuermchen).
        pfad_str = file_path.as_posix()
        try:
            treffer = _re.search(feld.regex, pfad_str)
        except _re.error as e:
            logger.warning(
                "pfad_regex fuer '%s' ungueltig: %s", feld.ziel, e,
            )
            return None
        if not treffer:
            logger.warning(
                "pfad_regex '%s' findet nichts im Pfad '%s' (Feld '%s').",
                feld.regex, pfad_str, feld.ziel,
            )
            return None
        # Mehrere Gruppen zusammensetzen (z.B. Jahr+Monat -> "202604"),
        # oder Fallback auf die einzelne 'gruppe' fuer den einfachen Fall.
        if feld.gruppen:
            try:
                teile = [treffer.group(g) for g in feld.gruppen]
                return "".join(teile)
            except IndexError:
                logger.warning(
                    "pfad_regex fuer '%s': eine der Gruppen %s existiert "
                    "nicht im Match.", feld.ziel, feld.gruppen,
                )
                return None
        try:
            return treffer.group(feld.gruppe)
        except IndexError:
            logger.warning(
                "pfad_regex fuer '%s': Gruppe %d existiert nicht im Match.",
                feld.ziel, feld.gruppe,
            )
            return None

    raise ValueError(f"Unbekannte system_quelle '{quelle}'.")


# Sichere Ausdrucksauswertung: nur numerische Operationen erlaubt.
# KEIN eval() auf User-Input. Wir bauen einen kleinen Parser mit
# Python's ast-Modul und lassen nur die vier Grundrechenarten + Klammern
# und einfache Feldnamen zu.
def _auswerte_formel(formel: str, kontext: dict[str, Any]) -> Any:
    """Einfacher arithmetischer Ausdruck ueber gemappte Felder auswerten.

    Erlaubt sind: + - * / ( ) und Namen aus dem kontext-Dict. Alles
    andere wird abgewiesen. Wir vermeiden bewusst eval() aus
    Sicherheitsgruenden.
    """
    import ast as _ast

    baum = _ast.parse(formel, mode="eval")

    def eval_node(node):
        if isinstance(node, _ast.Expression):
            return eval_node(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(
                f"In berechneter Formel nicht erlaubt: {type(node.value).__name__}"
            )
        if isinstance(node, _ast.Name):
            if node.id not in kontext:
                raise ValueError(f"Unbekanntes Feld '{node.id}' in Formel.")
            wert = kontext[node.id]
            if wert is None:
                return 0
            return float(wert)
        if isinstance(node, _ast.BinOp):
            l = eval_node(node.left)
            r = eval_node(node.right)
            op = node.op
            if isinstance(op, _ast.Add):
                return l + r
            if isinstance(op, _ast.Sub):
                return l - r
            if isinstance(op, _ast.Mult):
                return l * r
            if isinstance(op, _ast.Div):
                return l / r
            raise ValueError(f"Operator nicht erlaubt: {type(op).__name__}")
        if isinstance(node, _ast.UnaryOp):
            if isinstance(node.op, _ast.USub):
                return -eval_node(node.operand)
            if isinstance(node.op, _ast.UAdd):
                return +eval_node(node.operand)
        raise ValueError(f"Ausdruck nicht erlaubt: {type(node).__name__}")

    return eval_node(baum)


def _mappe_iter(
    file_path: Path, cfg: QuellenConfig, ladezeit: datetime,
    nur_zeilen: set[int] | None = None,
) -> Iterator[tuple[dict[str, Any], str]]:
    """Alter Weg: regeln + abgeleitete_felder. Yield: (satz, roh_zeile)."""
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

    for nr, zeile in zeilen_iter(file_path, d):
        if not zeile:
            continue
        # Filter fuer partiellen Modus
        if nur_zeilen is not None and nr not in nur_zeilen:
            continue

        roh_zeile = d.trennzeichen.join(zeile)
        satz: dict[str, Any] = {}

        for regel in cfg.mapping.regeln:
            spalte = spalten_nach_name[regel.quelle]
            rohwert = zeile[spalte.position]
            satz[regel.ziel] = konvertiere(rohwert, spalte.typ)

        satz.update(abgeleitete_werte)
        yield satz, roh_zeile