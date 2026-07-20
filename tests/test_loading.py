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


def test_pk_konflikt_skip_behaelt_bestehenden_datensatz(
    testkatalog: Path, tmp_path: Path, db_pfad: Path, fixe_ladezeit: datetime
) -> None:
    """Default `pk_konflikt=skip`: gleiche PK -> bestehender Wert bleibt,
    neuer wird uebersprungen."""
    cfg = load_catalog(testkatalog).configs["oe_preise"]

    v1 = tmp_path / "v1.txt"
    v1.write_text("Toyota|ABC123|Bremse|10.00|2026-01-15\n", encoding="utf-8")
    lade_sqlite(mappe(v1, cfg, ladezeit=fixe_ladezeit), cfg, db_pfad)

    v2 = tmp_path / "v2.txt"
    v2.write_text("Toyota|ABC123|Bremse|99.99|2026-01-15\n", encoding="utf-8")
    r = lade_sqlite(mappe(v2, cfg, ladezeit=fixe_ladezeit), cfg, db_pfad)

    # Neuer Satz wurde uebersprungen
    assert r.zeilen_geladen == 0
    assert r.zeilen_uebersprungen == 1

    con = sqlite3.connect(db_pfad)
    try:
        preis = con.execute(
            "SELECT preis FROM oe_preise WHERE oeno = 'ABC123'"
        ).fetchone()[0]
    finally:
        con.close()
    # Alter Wert bleibt erhalten
    assert preis == 10.00


def test_pk_konflikt_update_ueberschreibt(
    tmp_path: Path, fixe_ladezeit: datetime
) -> None:
    """Explizit pk_konflikt=update: bestehender Datensatz wird aktualisiert."""
    from textwrap import dedent
    from vierol_import.catalog.reader import load_catalog

    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "src.yaml").write_text(
        dedent(
            """
            name: src
            datei: {trennzeichen: "|"}
            spalten:
              - {position: 0, name: k, typ: string}
              - {position: 1, name: v, typ: integer}
            mapping:
              regeln:
                - {quelle: k, ziel: k}
                - {quelle: v, ziel: v}
            zielsystem:
              typ: sqlite
              tabelle: t
              upsert_key: [k]
              pk_konflikt: update
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = load_catalog(catalog).configs["src"]
    db = tmp_path / "test.sqlite"

    f1 = tmp_path / "f1.txt"; f1.write_text("A|1\n", encoding="utf-8")
    lade_sqlite(mappe(f1, cfg, ladezeit=fixe_ladezeit), cfg, db)

    f2 = tmp_path / "f2.txt"; f2.write_text("A|99\n", encoding="utf-8")
    r = lade_sqlite(mappe(f2, cfg, ladezeit=fixe_ladezeit), cfg, db)

    assert r.zeilen_geladen == 1
    assert r.zeilen_uebersprungen == 0
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT v FROM t WHERE k='A'").fetchone()[0] == 99
    finally:
        con.close()


def test_pk_konflikt_reject_wirft_und_macht_rollback(
    tmp_path: Path, fixe_ladezeit: datetime
) -> None:
    """pk_konflikt=reject: erster Konflikt -> PKKonfliktFehler + Rollback,
    nichts von der zweiten Datei wird geladen."""
    from textwrap import dedent
    from vierol_import.catalog.reader import load_catalog
    from vierol_import.loading.loader import PKKonfliktFehler
    import pytest as _pytest

    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "src.yaml").write_text(
        dedent(
            """
            name: src
            datei: {trennzeichen: "|"}
            spalten:
              - {position: 0, name: k, typ: string}
              - {position: 1, name: v, typ: integer}
            mapping:
              regeln:
                - {quelle: k, ziel: k}
                - {quelle: v, ziel: v}
            zielsystem:
              typ: sqlite
              tabelle: t
              upsert_key: [k]
              pk_konflikt: reject
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = load_catalog(catalog).configs["src"]
    db = tmp_path / "test.sqlite"

    f1 = tmp_path / "f1.txt"; f1.write_text("A|1\nB|2\n", encoding="utf-8")
    lade_sqlite(mappe(f1, cfg, ladezeit=fixe_ladezeit), cfg, db)

    # Zweite Datei enthaelt einen bestehenden PK (A) und einen neuen (C).
    # Modus reject -> beides wird zurueckgerollt, C darf NICHT auftauchen.
    f2 = tmp_path / "f2.txt"; f2.write_text("A|100\nC|3\n", encoding="utf-8")
    with _pytest.raises(PKKonfliktFehler):
        lade_sqlite(mappe(f2, cfg, ladezeit=fixe_ladezeit), cfg, db)

    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        assert con.execute("SELECT v FROM t WHERE k='A'").fetchone()[0] == 1
    finally:
        con.close()