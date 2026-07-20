"""
End-to-End-Tests der ImportEngine.

Diese Tests decken den kompletten Weg Datei -> SQLite ab und sind
das strengste Sicherheitsnetz gegen Regressionen: wenn ein einzelnes
Modul refactored wird, muessen diese Tests weiter gruen sein.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from vierol_import.catalog.reader import load_catalog
from vierol_import.engine import ImportEngine, Status


@pytest.fixture
def engine(testkatalog: Path, db_pfad: Path, fixe_ladezeit: datetime) -> ImportEngine:
    configs = load_catalog(testkatalog).configs
    return ImportEngine(configs, db_pfad=db_pfad, ladezeit=fixe_ladezeit)


def test_auto_gute_datei_wird_geladen(
    engine: ImportEngine, gute_datei: Path, db_pfad: Path
) -> None:
    e = engine.verarbeite_auto_und_schreibe(gute_datei)

    assert e.erfolg
    assert e.status is Status.GELADEN
    assert e.quelle == "oe_preise"
    assert e.zeilen_geladen == 3

    con = sqlite3.connect(db_pfad)
    try:
        assert con.execute("SELECT COUNT(*) FROM oe_preise").fetchone()[0] == 3
    finally:
        con.close()


def test_auto_unbekannte_datei_wird_abgelehnt(
    engine: ImportEngine, unbekannte_datei: Path
) -> None:
    e = engine.verarbeite_auto_und_schreibe(unbekannte_datei)
    assert not e.erfolg
    assert e.status is Status.ABGELEHNT_UNBEKANNT
    assert e.quelle is None


def test_auto_kaputte_datei_wird_abgelehnt_wegen_score(
    engine: ImportEngine, kaputte_datei: Path
) -> None:
    e = engine.verarbeite_auto_und_schreibe(kaputte_datei)
    assert not e.erfolg
    assert e.status is Status.ABGELEHNT_UNSICHER
    assert "Schwellenwert" in e.fehler_grund


def test_mit_quelle_bereitet_nur_vor_und_laedt_nicht(
    engine: ImportEngine, gute_datei: Path, db_pfad: Path
) -> None:
    """Two-Phase: verarbeite_mit_quelle allein darf NICHT schreiben."""
    e = engine.verarbeite_mit_quelle(gute_datei, "oe_preise")

    assert e.bereit_zum_schreiben
    assert e.zeilen_geladen == 0            # noch nicht geschrieben
    assert e.mapping is not None
    assert len(e.mapping.saetze) == 3       # Mapping steht bereit

    # Zieltabelle darf noch gar nicht existieren
    con = sqlite3.connect(db_pfad)
    try:
        tabellen = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        con.close()
    assert tabellen == []


def test_schreibe_nach_vorbereitung_persistiert(
    engine: ImportEngine, gute_datei: Path, db_pfad: Path
) -> None:
    e = engine.verarbeite_mit_quelle(gute_datei, "oe_preise")
    engine.schreibe(e)

    assert e.erfolg
    assert e.zeilen_geladen == 3
    con = sqlite3.connect(db_pfad)
    try:
        assert con.execute("SELECT COUNT(*) FROM oe_preise").fetchone()[0] == 3
    finally:
        con.close()


def test_schreibe_ohne_vorbereitung_ist_programmierfehler(
    engine: ImportEngine, gute_datei: Path
) -> None:
    from vierol_import.engine import VerarbeitungsErgebnis
    e = VerarbeitungsErgebnis(datei=gute_datei, status=Status.ABGELEHNT_UNBEKANNT)
    with pytest.raises(ValueError, match="bereit_zum_schreiben"):
        engine.schreibe(e)


def test_mit_quelle_lehnt_kaputte_daten_ab(
    engine: ImportEngine, kaputte_datei: Path
) -> None:
    """Auch im interaktiven Modus greift die Validierung -> nichts zu schreiben."""
    e = engine.verarbeite_mit_quelle(kaputte_datei, "oe_preise")
    assert not e.bereit_zum_schreiben
    assert e.status is Status.ABGELEHNT_UNGUELTIG
    assert len(e.fehler_details) > 0


def test_unbekannte_quelle_ist_programmierfehler(
    engine: ImportEngine, gute_datei: Path
) -> None:
    with pytest.raises(ValueError, match="nicht im Katalog"):
        engine.verarbeite_mit_quelle(gute_datei, "gibt_es_nicht")


def test_leere_config_ist_verboten(db_pfad: Path) -> None:
    with pytest.raises(ValueError, match="mindestens eine Config"):
        ImportEngine({}, db_pfad=db_pfad)