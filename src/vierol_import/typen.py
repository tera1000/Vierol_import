"""
Zentrale Typ-Logik: Pruefen und Konvertieren von Zell-Werten.

Wird von drei Pipeline-Stufen gemeinsam genutzt:
  - Erkennung:   passt_zelle()  — "koennte das ein Wert dieser Spalte sein?"
  - Validierung: passt_zelle() + konvertiere() fuer Wertebereichs-Pruefung
  - Mapping:     konvertiere()  — Zell-String -> Python-Wert

Eine einzige Definition pro Typ verhindert, dass Erkennung und
Validierung unterschiedlicher Meinung sind, was z. B. eine gueltige
deutsche Dezimalzahl ist.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from vierol_import.catalog.schema import SpaltenDef

# --- Muster pro Typ -----------------------------------------------------------

_RE_INTEGER = re.compile(r"^-?\d+$")
_RE_DECIMAL_DE = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$|^-?\d+(,\d+)?$")
_RE_DECIMAL_EN = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_RE_DATE_DMY = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
_RE_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BOOL_WAHR = {"1", "ja", "true", "y", "j"}
_BOOL_FALSCH = {"0", "nein", "false", "n"}


def passt_typ(wert: str, typ: str) -> bool:
    """Rein syntaktische Pruefung: sieht der String wie dieser Typ aus?

    Bewusst ohne Wertebereich (min/max) — das ist Aufgabe der
    Validierung. Die Erkennung soll nur die STRUKTUR beurteilen.
    """
    w = wert.strip()
    if typ == "string":
        return True
    if typ == "integer":
        return bool(_RE_INTEGER.match(w))
    if typ == "decimal_de":
        return bool(_RE_DECIMAL_DE.match(w))
    if typ == "decimal_en":
        return bool(_RE_DECIMAL_EN.match(w))
    if typ == "date_dmy":
        return bool(_RE_DATE_DMY.match(w)) and _datum_existiert(w, "%d.%m.%Y")
    if typ == "date_iso":
        return bool(_RE_DATE_ISO.match(w)) and _datum_existiert(w, "%Y-%m-%d")
    if typ == "boolean":
        return w.lower() in (_BOOL_WAHR | _BOOL_FALSCH)
    return False


def _datum_existiert(w: str, fmt: str) -> bool:
    """'2026-02-31' matcht das Regex, ist aber kein Datum."""
    try:
        datetime.strptime(w, fmt)
        return True
    except ValueError:
        return False


def passt_zelle(wert: str, spalte: SpaltenDef) -> bool:
    """Typ- UND Muster-Pruefung einer Zelle gegen ihre Spaltendefinition.

    Leere Zellen: bei Pflichtspalten ein Nicht-Treffer, bei optionalen
    Spalten neutral-positiv (sie sprechen nicht GEGEN die Quelle).
    """
    w = wert.strip()
    if not w:
        return not spalte.pflicht

    if not passt_typ(w, spalte.typ):
        return False

    if spalte.muster is not None and not re.match(spalte.muster, w):
        return False

    return True


def konvertiere(wert: str, typ: str) -> Any:
    """Zell-String -> Python-Wert. Setzt voraus, dass passt_typ() wahr war.

    Rueckgabetypen: integer -> int, decimal_* -> float,
    date_* -> datetime.date, boolean -> bool, string -> str (getrimmt).
    Leere Strings -> None.
    """
    w = wert.strip()
    if not w:
        return None

    if typ == "integer":
        return int(w)
    if typ == "decimal_de":
        return float(w.replace(".", "").replace(",", "."))
    if typ == "decimal_en":
        return float(w.replace(",", ""))
    if typ == "date_dmy":
        return datetime.strptime(w, "%d.%m.%Y").date()
    if typ == "date_iso":
        return date.fromisoformat(w)
    if typ == "boolean":
        return w.lower() in _BOOL_WAHR
    return w