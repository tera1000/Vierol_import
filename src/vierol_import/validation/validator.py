"""
Validierung: die GESAMTE Datei gegen die Spaltendefinitionen pruefen.

Abgrenzung zur Erkennung:
  - Erkennung:   Stichprobe, Frage "welche Quelle ist das wohl?"
  - Validierung: ganze Datei, Frage "ist diese Datei gut genug zum Laden?"

Beide nutzen dieselben Spaltendefinitionen und dieselbe Typ-Logik aus
`typen.py` — sie koennen sich also nie widersprechen.

Die Validierung sammelt zeilengenaue Fehler (Zeile, Spalte, Wert,
Grund) fuer den Reject-Bericht, bricht aber nach `max_fehler` ab:
Bei einer Datei mit 100.000 kaputten Zeilen sind die ersten 50 Fehler
aussagekraeftig genug, und der Bericht bleibt lesbar.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.encoding_erkennung import erkenne_encoding
from vierol_import.typen import konvertiere, passt_zelle

logger = logging.getLogger(__name__)

MAX_FEHLER = 50


@dataclass
class ValidierungsFehler:
    zeile: int          # 1-basiert, wie im Editor angezeigt
    spalte: str | None  # logischer Spaltenname, None bei Zeilen-Fehlern
    wert: str | None
    grund: str

    def __str__(self) -> str:
        if self.spalte is None:
            return f"Zeile {self.zeile}: {self.grund}"
        return f"Zeile {self.zeile}, Spalte '{self.spalte}': '{self.wert}' — {self.grund}"


@dataclass
class ValidierungsErgebnis:
    ok: bool
    zeilen_gesamt: int = 0
    zeilen_fehlerhaft: int = 0
    fehler: list[ValidierungsFehler] = field(default_factory=list)
    abgebrochen: bool = False  # True: mehr Fehler als MAX_FEHLER
    # Fuer partiellen Modus: nummern der ZEILEN, die durchgekommen sind
    # (1-basiert wie bei den Fehler-Zeilennummern).
    gute_zeilen: set[int] = field(default_factory=set)


def validiere(
    file_path: Path, cfg: QuellenConfig, partiell: bool = False
) -> ValidierungsErgebnis:
    """Datei vollstaendig gegen die Quellen-Config pruefen.

    Prueft pro Zeile: Spaltenanzahl; pro Zelle: Typ, Regex-Muster,
    Pflichtfeld, Wertebereich (minimum/maximum bei numerischen Typen).

    Mit `partiell=True` verhaelt sich der Validator etwas anders:
      - MAX_FEHLER-Grenze wird ignoriert (wir muessen alle Zeilen kennen),
      - `gute_zeilen` wird gefuellt mit den Nummern der validen Zeilen,
      - `ok` bleibt False, sobald es auch nur einen Fehler gab,
        aber der Aufrufer kann trotzdem die guten Zeilen laden.
    """
    ergebnis = ValidierungsErgebnis(ok=True)
    spalten = sorted(cfg.spalten, key=lambda s: s.position)
    d = cfg.datei

    try:
        encoding = erkenne_encoding(file_path, wunsch=d.encoding)
        f = open(file_path, newline="", encoding=encoding)
    except OSError as e:
        ergebnis.ok = False
        ergebnis.fehler.append(
            ValidierungsFehler(zeile=0, spalte=None, wert=None, grund=f"Datei nicht lesbar: {e}")
        )
        return ergebnis

    with f:
        reader = csv.reader(f, delimiter=d.trennzeichen)
        start_zeile = 2 if d.hat_header else 1
        if d.hat_header:
            next(reader, None)

        for nr, zeile in enumerate(reader, start=start_zeile):
            if not zeile:
                continue
            ergebnis.zeilen_gesamt += 1
            zeilen_fehler = _pruefe_zeile(nr, zeile, spalten)

            if zeilen_fehler:
                ergebnis.zeilen_fehlerhaft += 1
                if partiell:
                    # Alle Fehler sammeln, nicht abbrechen — der Aufrufer
                    # braucht die vollstaendige Liste fuer die Quarantaene.
                    ergebnis.fehler.extend(zeilen_fehler)
                else:
                    platz = MAX_FEHLER - len(ergebnis.fehler)
                    ergebnis.fehler.extend(zeilen_fehler[:platz])
                    if len(ergebnis.fehler) >= MAX_FEHLER:
                        ergebnis.abgebrochen = True
                        break
            else:
                ergebnis.gute_zeilen.add(nr)

    ergebnis.ok = ergebnis.zeilen_fehlerhaft == 0 and ergebnis.zeilen_gesamt > 0
    if ergebnis.zeilen_gesamt == 0:
        ergebnis.fehler.append(
            ValidierungsFehler(zeile=0, spalte=None, wert=None, grund="Datei enthaelt keine Datenzeilen.")
        )

    logger.info(
        "Validierung %s: %d Zeilen, %d fehlerhaft -> %s",
        file_path.name,
        ergebnis.zeilen_gesamt,
        ergebnis.zeilen_fehlerhaft,
        "OK" if ergebnis.ok else "ABGELEHNT",
    )
    return ergebnis


def _pruefe_zeile(nr, zeile, spalten) -> list[ValidierungsFehler]:
    fehler: list[ValidierungsFehler] = []

    if len(zeile) != len(spalten):
        return [
            ValidierungsFehler(
                zeile=nr,
                spalte=None,
                wert=None,
                grund=f"Spaltenanzahl {len(zeile)} statt {len(spalten)}",
            )
        ]

    for sp in spalten:
        wert = zeile[sp.position]
        w = wert.strip()

        if not w:
            if sp.pflicht:
                fehler.append(
                    ValidierungsFehler(nr, sp.name, wert, "Pflichtfeld ist leer")
                )
            continue

        if not passt_zelle(w, sp):
            fehler.append(
                ValidierungsFehler(
                    nr, sp.name, wert, f"passt nicht zu Typ '{sp.typ}'"
                    + (f" / Muster '{sp.muster}'" if sp.muster else "")
                )
            )
            continue

        # Wertebereich nur pruefen, wenn der Typ numerisch und der Wert
        # bereits als typkonform erkannt ist.
        if sp.typ in ("integer", "decimal_de", "decimal_en") and (
            sp.minimum is not None or sp.maximum is not None
        ):
            zahl = konvertiere(w, sp.typ)
            if sp.minimum is not None and zahl < sp.minimum:
                fehler.append(
                    ValidierungsFehler(nr, sp.name, wert, f"kleiner als Minimum {sp.minimum}")
                )
            if sp.maximum is not None and zahl > sp.maximum:
                fehler.append(
                    ValidierungsFehler(nr, sp.name, wert, f"groesser als Maximum {sp.maximum}")
                )

    return fehler