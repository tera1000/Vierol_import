"""Load: Insert, Upsert-Verhalten, Rollback bei Fehler."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from vierol_import.catalog.reader import load_catalog
from vierol_import.loading.loader import lade_sqlite
from vierol_import.mapping.mapper import mappe


def _zeilen_zaehlen(db: Path, tabelle: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{tabelle}"').fetchone()[0]
    finally:
        con.close()


def test_laden_erzeugt_tabelle_und_zeilen(
    testkatalog: Path, gute_datei: Path, db_pfad: Path, fixe_ladezeit: datetime
) -> None:
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    m = mappe(gute_datei, cfg, ladezeit=fixe_ladezeit)
    ergebnis = lade_sqlite(m, cfg, db_pfad)

    assert ergebnis.zeilen_geladen == 3
    assert _zeilen_zaehlen(db_pfad, "oe_preise") == 3


def test_upsert_erzeugt_keine_duplikate(
    testkatalog: Path, gute_datei: Path, db_pfad: Path, fixe_ladezeit: datetime
) -> None:
    """Zweimal dieselben Daten laden -> immer noch 3 Zeilen (Upsert)."""
    cfg = load_catalog(testkatalog).configs["oe_preise"]
    m = mappe(gute_datei, cfg, ladezeit=fixe_ladezeit)

    lade_sqlite(m, cfg, db_pfad)
    lade_sqlite(m, cfg, db_pfad)

    assert _zeilen_zaehlen(db_pfad, "oe_preise") == 3


def test_upsert_aktualisiert_bestehende_datensaetze(
    testkatalog: Path, tmp_path: Path, db_pfad: Path, fixe_ladezeit: datetime
) -> None:
    """Gleicher Upsert-Key + geaenderter Preis -> Preis wird ueberschrieben."""
    cfg = load_catalog(testkatalog).configs["oe_preise"]

    v1 = tmp_path / "v1.txt"
    v1.write_text("Toyota|ABC123|Bremse|10.00|2026-01-15\n", encoding="utf-8")
    lade_sqlite(mappe(v1, cfg, ladezeit=fixe_ladezeit), cfg, db_pfad)

    v2 = tmp_path / "v2.txt"
    v2.write_text("Toyota|ABC123|Bremse|99.99|2026-01-15\n", encoding="utf-8")
    lade_sqlite(mappe(v2, cfg, ladezeit=fixe_ladezeit), cfg, db_pfad)

    con = sqlite3.connect(db_pfad)
    try:
        preis = con.execute(
            "SELECT preis FROM oe_preise WHERE oeno = 'ABC123'"
        ).fetchone()[0]
    finally:
        con.close()
    assert preis == 99.99