"""Mapping: Typkonvertierung + abgeleitete Felder."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from vierol_import.catalog.reader import load_catalog
from vierol_import.mapping.mapper import mappe


def test_typkonvertierung(testkatalog: Path, gute_datei: Path, fixe_ladezeit: datetime) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    r = mappe(gute_datei, cfg, ladezeit=fixe_ladezeit)

    assert len(r.saetze) == 3
    ersten_satz = r.saetze[0]
    assert ersten_satz["hersteller"] == "Toyota"           # string
    assert ersten_satz["preis"] == 12.5                    # decimal_en -> float
    assert isinstance(ersten_satz["preis"], float)
    assert ersten_satz["stichtag"] == date(2026, 1, 15)    # date_iso -> date


def test_abgeleitetes_feld_jahrmonat(
    testkatalog: Path, gute_datei: Path, fixe_ladezeit: datetime
) -> None:
    """Alle Zeilen bekommen denselben Ladezeitpunkt-basierten Wert."""
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    r = mappe(gute_datei, cfg, ladezeit=fixe_ladezeit)

    for satz in r.saetze:
        assert satz["jahrmonat"] == "202607"


def test_zielfeld_reihenfolge_ist_deterministisch(
    testkatalog: Path, gute_datei: Path, fixe_ladezeit: datetime
) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    r = mappe(gute_datei, cfg, ladezeit=fixe_ladezeit)
    # Erst Spalten-Regeln (in YAML-Reihenfolge), dann abgeleitete Felder
    assert r.zielfelder == [
        "hersteller", "oeno", "bezeichnung", "preis", "stichtag", "jahrmonat"
    ]