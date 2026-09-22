"""Validierung: gueltige Datei ok, fehlerhafte Datei mit zeilengenauen Fehlern."""

from __future__ import annotations

from pathlib import Path

from vierol_import.catalog.reader import load_catalog
from vierol_import.validation.validator import validiere


def test_gueltige_datei_akzeptiert(testkatalog: Path, gute_datei: Path) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    e = validiere(gute_datei, cfg)
    assert e.ok
    assert e.zeilen_gesamt == 3
    assert e.zeilen_fehlerhaft == 0
    assert e.fehler == []


def test_kaputte_datei_findet_alle_fehler(
    testkatalog: Path, kaputte_datei: Path
) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    e = validiere(kaputte_datei, cfg)

    assert not e.ok
    assert e.zeilen_gesamt == 4
    assert e.zeilen_fehlerhaft == 3

    gruende = " | ".join(str(f) for f in e.fehler)
    assert "Minimum" in gruende          # negativer Preis
    assert "date_iso" in gruende          # 2026-13-45
    assert "Spaltenanzahl" in gruende     # zu wenige Spalten


def test_leere_datei_ist_nicht_ok(tmp_path: Path, testkatalog: Path) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    leer = tmp_path / "leer.txt"
    leer.write_text("", encoding="utf-8")
    e = validiere(leer, cfg)
    assert not e.ok
    assert e.zeilen_gesamt == 0