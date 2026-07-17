"""
Gemeinsame Fixtures fuer alle Tests.

`tmp_path` von pytest gibt uns pro Test ein frisches Verzeichnis —
darin bauen wir uns einen Mini-Katalog und Mini-Datendateien, damit
jeder Test in Isolation laeuft (keine gemeinsam genutzte SQLite, kein
verbleibender Zustand).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def testkatalog(tmp_path: Path) -> Path:
    """Katalog mit EINER Quelle 'oe_preise' (5 Spalten, Pipe, headerlos)."""
    catalog = tmp_path / "config_catalog"
    catalog.mkdir()
    (catalog / "oe_preise.yaml").write_text(
        dedent(
            """
            name: oe_preise
            beschreibung: "Testquelle fuer Unit-Tests"

            datei:
              trennzeichen: "|"
              encoding: "utf-8"
              hat_header: false

            spalten:
              - {position: 0, name: hersteller, typ: string}
              - {position: 1, name: oeno,       typ: string, muster: "^[A-Z0-9]+$"}
              - {position: 2, name: bezeichnung, typ: string}
              - {position: 3, name: preis,      typ: decimal_en, minimum: 0}
              - {position: 4, name: stichtag,   typ: date_iso}

            klassifikation:
              stichprobe_zeilen: 20
              schwellenwert: 0.9

            mapping:
              regeln:
                - {quelle: hersteller,  ziel: hersteller}
                - {quelle: oeno,        ziel: oeno}
                - {quelle: bezeichnung, ziel: bezeichnung}
                - {quelle: preis,       ziel: preis}
                - {quelle: stichtag,    ziel: stichtag}
              abgeleitete_felder:
                - {ziel: jahrmonat, funktion: ladezeitpunkt_jahrmonat}

            zielsystem:
              typ: sqlite
              tabelle: oe_preise
              upsert_key: [hersteller, oeno]
            """
        ).strip(),
        encoding="utf-8",
    )
    return catalog


@pytest.fixture
def gute_datei(tmp_path: Path) -> Path:
    p = tmp_path / "gut.txt"
    p.write_text(
        "Toyota|ABC123|Bremse|12.50|2026-01-15\n"
        "Honda|XYZ999|Filter|4.20|2026-02-01\n"
        "Toyota|DEF456|Kupplung|89.90|2026-03-10\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def kaputte_datei(tmp_path: Path) -> Path:
    """Zeile 1 ok, Zeile 2 negativer Preis, Zeile 3 kaputtes Datum,
    Zeile 4 zu wenige Spalten."""
    p = tmp_path / "kaputt.txt"
    p.write_text(
        "Toyota|ABC123|Bremse|12.50|2026-01-15\n"
        "Toyota|XYZ|Test|-5.00|2026-01-15\n"
        "Toyota|DEF|Test|10.00|2026-13-45\n"
        "Toyota|GHI|Test|10.00\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def unbekannte_datei(tmp_path: Path) -> Path:
    """Falsches Trennzeichen und falsche Spaltenzahl."""
    p = tmp_path / "unbekannt.txt"
    p.write_text("kunde_a,2026,offen\nkunde_b,2025,bezahlt\n", encoding="utf-8")
    return p


@pytest.fixture
def db_pfad(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite"


@pytest.fixture
def fixe_ladezeit() -> datetime:
    """Fixierter Zeitstempel — macht Tests auf `jahrmonat` deterministisch."""
    return datetime(2026, 7, 17, 10, 30)