"""
Beispiel-Test fuer den Klassifikator.

Zeigt das Muster: Test-Daten anlegen, Klassifikator laufen lassen,
Ergebnis pruefen. Alle weiteren Tests folgen dem gleichen Aufbau.
"""

from pathlib import Path

from vierol_import.catalog.reader import CatalogReader
from vierol_import.classification.classifier import Classifier


def test_warenkorb_wird_erkannt(tmp_path: Path) -> None:
    # Katalog vorbereiten
    catalog_dir = Path("config_catalog")
    catalog = CatalogReader(catalog_dir)

    # Beispiel-Datei bereitstellen
    sample = tmp_path / "warenkorb_test.csv"
    sample.write_text(
        "ArtNr;Menge;KundenNr;Datum\nA-1;1;K-1;01.01.2026\n",
        encoding="utf-8",
    )

    # Klassifizieren
    classifier = Classifier(catalog)
    result = classifier.classify(sample)

    assert result.configuration is not None
    assert result.configuration.name == "warenkorb"
    assert result.score >= 0.8
