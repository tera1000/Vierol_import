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

--- roh_zeile im Fehler ---
Jeder ValidierungsFehler traegt zusaetzlich die vollstaendige
Original-Datei-Zeile (rekonstruiert per trennzeichen.join). Damit
landet sie ueber das Duck-Typing in audit_log auch in der
import_fehler.roh_zeile-Spalte — analog zu OracleBatchFehler.roh_zeile.

--- Datei-Format ---
Der Datei-Zugriff geht ueber vierol_import.reader.zeilen_iter — das
ist die zentrale Format-Abstraktion und funktioniert einheitlich
fuer CSV, Excel, XML und JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.catalog.datei_reader import zeilen_iter
from vierol_import.typen import konvertiere, passt_zelle

logger = logging.getLogger(__name__)

MAX_FEHLER = 50

# Wie viele Zeichen eines zu langen Werts wir in der Fehlermeldung/DB
# festhalten. Ein 18.000-Zeichen-Wert soll die Fehlerliste nicht sprengen —
# die tatsaechliche Laenge steht ohnehin im Grund-Text.
MAX_WERT_ANZEIGE = 100


@dataclass
class ValidierungsFehler:
    zeile: int          # 1-basiert, wie im Editor angezeigt
    spalte: str | None  # logischer Spaltenname, None bei Zeilen-Fehlern
    wert: str | None
    grund: str
    # Original-Datei-Zeile fuer die Fehler-Diagnose im Kontext.
    # Wird ueber Duck-Typing in audit_log ausgelesen und landet in
    # import_fehler.roh_zeile.
    roh_zeile: str | None = None

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
    Pflichtfeld, maximale Zeichenlaenge, Wertebereich (minimum/maximum
    bei numerischen Typen).

    Mit `partiell=True` verhaelt sich der Validator etwas anders:
      - MAX_FEHLER-Grenze wird ignoriert (wir muessen alle Zeilen kennen),
      - `gute_zeilen` wird gefuellt mit den Nummern der validen Zeilen,
      - `ok` bleibt False, sobald es auch nur einen Fehler gab,
        aber der Aufrufer kann trotzdem die guten Zeilen laden.
    """
    ergebnis = ValidierungsErgebnis(ok=True)
    spalten = sorted(cfg.spalten, key=lambda s: s.position)
    d = cfg.datei

    # Datei-Zugriff ueber den zentralen Reader — der kuemmert sich um
    # das Format (CSV/Excel/XML/JSON) und liefert einen einheitlichen
    # Iterator von (zeilennummer, felder). Fehler beim Oeffnen kommen
    # als Exception hoch und werden hier aufgefangen.
    try:
        iterator = zeilen_iter(file_path, d)
    except Exception as e:
        ergebnis.ok = False
        ergebnis.fehler.append(
            ValidierungsFehler(
                zeile=0, spalte=None, wert=None,
                grund=f"Datei nicht lesbar: {type(e).__name__}: {e}",
            )
        )
        return ergebnis

    for nr, zeile in iterator:
        if not zeile:
            continue
        ergebnis.zeilen_gesamt += 1
        # Original-Datei-Zeile rekonstruieren (per trennzeichen.join
        # der geparsedten Felder). Wird pro erstelltem
        # ValidierungsFehler mitgegeben. Bei Excel/XML/JSON ist das
        # eine synthetische, aber lesbare Darstellung der Zeile.
        roh_zeile = d.trennzeichen.join(zeile)
        zeilen_fehler = _pruefe_zeile(nr, zeile, spalten, roh_zeile)

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


def _pruefe_zeile(nr, zeile, spalten, roh_zeile: str) -> list[ValidierungsFehler]:
    """Eine Datei-Zeile gegen die Spaltendefinitionen pruefen.

    `roh_zeile` wird auf jeden generierten Fehler gesetzt, damit der
    Aufrufer (Audit-Log) die Original-Datei-Zeile mitprotokollieren
    kann.
    """
    fehler: list[ValidierungsFehler] = []

    if len(zeile) != len(spalten):
        return [
            ValidierungsFehler(
                zeile=nr,
                spalte=None,
                wert=None,
                grund=f"Spaltenanzahl {len(zeile)} statt {len(spalten)}",
                roh_zeile=roh_zeile,
            )
        ]

    for sp in spalten:
        wert = zeile[sp.position]
        w = wert.strip()

        if not w:
            if sp.pflicht:
                fehler.append(
                    ValidierungsFehler(
                        nr, sp.name, wert, "Pflichtfeld ist leer",
                        roh_zeile=roh_zeile,
                    )
                )
            continue

        # Maximale Zeichenlaenge pruefen — unabhaengig vom Typ, greift
        # z.B. bei string-Spalten, die eine Ziel-DB-Spaltenlaenge
        # spiegeln (Oracle VARCHAR2(350) etc.). Diese Pruefung laeuft
        # VOR der Typ-/Muster-Pruefung, damit ein zu langer Wert nicht
        # zusaetzlich noch als Typ-Fehler auftaucht.
        if sp.max_laenge is not None and len(w) > sp.max_laenge:
            anzeige = w if len(w) <= MAX_WERT_ANZEIGE else w[:MAX_WERT_ANZEIGE] + "..."
            fehler.append(
                ValidierungsFehler(
                    nr, sp.name, anzeige,
                    f"Wert zu lang ({len(w)} Zeichen, Maximum {sp.max_laenge})",
                    roh_zeile=roh_zeile,
                )
            )
            continue

        if not passt_zelle(w, sp):
            fehler.append(
                ValidierungsFehler(
                    nr, sp.name, wert, f"ungueltiger Wert Typ '{sp.typ}'"
                    + (f" / Muster '{sp.muster}'" if sp.muster else ""),
                    roh_zeile=roh_zeile,
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
                    ValidierungsFehler(
                        nr, sp.name, wert,
                        f"kleiner als Minimum {sp.minimum}",
                        roh_zeile=roh_zeile,
                    )
                )
            if sp.maximum is not None and zahl > sp.maximum:
                fehler.append(
                    ValidierungsFehler(
                        nr, sp.name, wert,
                        f"groesser als Maximum {sp.maximum}",
                        roh_zeile=roh_zeile,
                    )
                )

    return fehler