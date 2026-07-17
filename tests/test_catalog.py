"""Katalog-Tests: laedt gueltige Configs, faengt fehlerhafte ab."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from vierol_import.catalog.reader import load_catalog


def test_gueltiger_katalog_wird_geladen(testkatalog: Path) -> None:
    result = load_catalog(testkatalog)
    assert result.ok
    assert "oe_preise" in result.configs
    cfg = result.configs["oe_preise"]
    assert cfg.spalten_anzahl == 5
    assert cfg.datei.trennzeichen == "|"
    assert not cfg.datei.hat_header
    assert cfg.zielsystem.upsert_key == ["hersteller", "oeno"]


def test_fehlende_pflichtfelder_werden_erkannt(tmp_path: Path) -> None:
    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "kaputt.yaml").write_text("name: kaputt\n", encoding="utf-8")

    result = load_catalog(catalog)
    assert not result.ok
    assert "kaputt" in result.fehler
    # Pflichtfelder aus dem Meta-Schema muessen als fehlend gemeldet werden
    joined = " ".join(result.fehler["kaputt"])
    for feld in ("spalten", "mapping", "zielsystem"):
        assert feld in joined


def test_unbekanntes_feld_wird_verboten(tmp_path: Path) -> None:
    """extra='forbid' — Tippfehler in YAML fallen sofort auf."""
    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "typo.yaml").write_text(
        dedent(
            """
            name: typo
            datei: {trennzeichen: "|"}
            spalten:
              - {position: 0, name: a, typ: string}
            klassifikation: {schwellenwert: 0.9}
            mapping:
              regeln:
                - {quelle: a, ziel: a}
            zielsystem: {typ: sqlite, tabelle: t}
            ungueltiges_extra_feld: kaputt
            """
        ).strip(),
        encoding="utf-8",
    )
    result = load_catalog(catalog)
    assert "typo" in result.fehler


def test_mapping_regel_zeigt_auf_unbekannte_spalte(tmp_path: Path) -> None:
    """Cross-Validation: Mapping.quelle muss eine echte Spalte sein."""
    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "x.yaml").write_text(
        dedent(
            """
            name: x
            datei: {trennzeichen: "|"}
            spalten:
              - {position: 0, name: a, typ: string}
            mapping:
              regeln:
                - {quelle: nicht_existent, ziel: b}
            zielsystem: {typ: sqlite, tabelle: t}
            """
        ).strip(),
        encoding="utf-8",
    )
    result = load_catalog(catalog)
    assert "x" in result.fehler
    assert any("nicht_existent" in f for f in result.fehler["x"])


def test_dateiname_muss_zum_namen_passen(tmp_path: Path) -> None:
    catalog = tmp_path / "cat"
    catalog.mkdir()
    (catalog / "falsch.yaml").write_text(
        dedent(
            """
            name: anders
            datei: {trennzeichen: "|"}
            spalten:
              - {position: 0, name: a, typ: string}
            mapping:
              regeln:
                - {quelle: a, ziel: a}
            zielsystem: {typ: sqlite, tabelle: t}
            """
        ).strip(),
        encoding="utf-8",
    )
    result = load_catalog(catalog)
    assert "falsch" in result.fehler