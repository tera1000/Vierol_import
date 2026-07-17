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
    e = engine.verarbeite_auto(gute_datei)

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
    e = engine.verarbeite_auto(unbekannte_datei)
    assert not e.erfolg
    assert e.status is Status.ABGELEHNT_UNBEKANNT
    assert e.quelle is None


def test_auto_kaputte_datei_wird_abgelehnt_wegen_score(
    engine: ImportEngine, kaputte_datei: Path
) -> None:
    """Kaputte Datei: Struktur passt teils, aber der Score liegt unter der
    Schwelle. Batch-Modus lehnt ab, statt zu raten."""
    e = engine.verarbeite_auto(kaputte_datei)
    assert not e.erfolg
    assert e.status is Status.ABGELEHNT_UNSICHER
    assert "Schwellenwert" in e.fehler_grund


def test_mit_quelle_ueberspringt_erkennung(
    engine: ImportEngine, gute_datei: Path
) -> None:
    e = engine.verarbeite_mit_quelle(gute_datei, "oe_preise")
    assert e.erfolg
    assert e.score is None  # keine Erkennung gelaufen


def test_mit_quelle_lehnt_kaputte_daten_ab(
    engine: ImportEngine, kaputte_datei: Path
) -> None:
    """Interaktiver Modus: User haette Quelle bestaetigt -> Validierung greift."""
    e = engine.verarbeite_mit_quelle(kaputte_datei, "oe_preise")
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