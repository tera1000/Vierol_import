"""
Laedt und validiert alle Quellen-Konfigurationen aus dem Config Catalog.

Der Katalog ist ein Verzeichnis mit einer YAML-Datei pro externer
Datenquelle (z. B. `config_catalog/topmotive.yaml`). Dieses Modul ist
die einzige Stelle, an der YAML-Configs gelesen werden — alle anderen
Pipeline-Stufen (Klassifikation, Validierung, Mapping, Load) bekommen
bereits validierte `QuellenConfig`-Objekte und muessen sich nie mehr
um YAML-Parsing oder fehlerhafte Configs kuemmern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from vierol_import.catalog.meta_schema import QuellenConfig

logger = logging.getLogger(__name__)


@dataclass
class CatalogLoadResult:
    """Ergebnis eines Katalog-Ladevorgangs: getrennt nach gueltigen
    Configs und Fehlern, damit `validate-config` einen vollstaendigen
    Bericht ausgeben kann statt beim ersten Fehler abzubrechen."""

    configs: dict[str, QuellenConfig] = field(default_factory=dict)
    fehler: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.fehler


def _lade_einzeldatei(pfad: Path) -> tuple[QuellenConfig | None, list[str]]:
    """Eine einzelne YAML-Datei laden und gegen das Meta-Schema pruefen.

    Gibt entweder (config, []) oder (None, [fehlermeldungen]) zurueck.
    Faengt sowohl YAML-Syntaxfehler als auch Pydantic-Validierungsfehler.
    """
    try:
        rohdaten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return None, [f"YAML-Syntaxfehler: {e}"]

    if not isinstance(rohdaten, dict):
        return None, ["Datei enthaelt kein YAML-Mapping auf oberster Ebene."]

    try:
        config = QuellenConfig(**rohdaten)
    except ValidationError as e:
        fehlermeldungen = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        return None, fehlermeldungen

    return config, []


def load_catalog(catalog_dir: Path) -> CatalogLoadResult:
    """Alle `*.yaml`-Dateien im Katalog-Verzeichnis laden und validieren.

    Der Dateiname (ohne Endung) muss mit dem `name`-Feld in der Config
    uebereinstimmen — das verhindert Verwechslungen, wenn jemand eine
    Datei umbenennt, ohne den Inhalt anzupassen.
    """
    result = CatalogLoadResult()

    yaml_dateien = sorted(catalog_dir.glob("*.yaml")) + sorted(
        catalog_dir.glob("*.yml")
    )

    if not yaml_dateien:
        logger.warning("Keine YAML-Dateien in %s gefunden.", catalog_dir)
        return result

    for pfad in yaml_dateien:
        key = pfad.stem
        config, fehler = _lade_einzeldatei(pfad)

        if config is not None and config.name != key:
            fehler.append(
                f"Dateiname '{pfad.name}' passt nicht zum 'name'-Feld "
                f"('{config.name}') in der Datei."
            )
            config = None

        if config is not None:
            result.configs[key] = config
        else:
            result.fehler[key] = fehler
            logger.error("Config '%s' ungueltig: %s", key, "; ".join(fehler))

    return result