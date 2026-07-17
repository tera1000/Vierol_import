"""Klassifikation: eindeutig / K.O. / unsicher."""

from __future__ import annotations

from pathlib import Path

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere


def test_eindeutige_datei_wird_erkannt(testkatalog: Path, gute_datei: Path) -> None:
    configs = load_catalog(testkatalog).configs
    ranking = klassifiziere(gute_datei, configs)
    assert ranking.bester is not None
    assert ranking.bester.quelle == "oe_preise"
    assert ranking.bester.score == 1.0


def test_falsches_trennzeichen_fuehrt_zu_ko(
    testkatalog: Path, unbekannte_datei: Path
) -> None:
    configs = load_catalog(testkatalog).configs
    ranking = klassifiziere(unbekannte_datei, configs)
    assert ranking.bester is None
    # Alle Ergebnisse muessen einen K.O.-Grund haben
    assert all(e.ko_grund for e in ranking.ergebnisse)


def test_optionale_spalten_zaehlen_nicht_gegen_score(
    tmp_path: Path, testkatalog: Path
) -> None:
    """Eine leere Zelle in einer Pflichtspalte druckt den Score."""
    configs = load_catalog(testkatalog).configs
    datei = tmp_path / "leer.txt"
    # Zweite Spalte (oeno, pflicht) leer
    datei.write_text("Toyota||Test|10.0|2026-01-01\n", encoding="utf-8")
    ranking = klassifiziere(datei, configs)
    assert ranking.bester is not None
    assert ranking.bester.score < 1.0