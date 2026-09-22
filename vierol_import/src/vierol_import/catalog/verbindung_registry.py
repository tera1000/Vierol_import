"""
Registry der Zielsystem-Verbindungen.

Analog zum Quellen-Katalog: alle YAML-Dateien im `zielsysteme/`-Ordner
werden eingelesen und gegen `Verbindung`-Schema validiert. Dateien mit
Unterstrich am Anfang (z.B. `_TEMPLATE.yaml`) werden uebersprungen.

Der Registry-Loader kennt die Quellen-Configs nicht direkt — die
Verknuepfung geschieht spaeter beim Import ueber
`cfg.zielsystem.ref -> Verbindung`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from vierol_import.catalog.verbindung_schema import Verbindung

logger = logging.getLogger(__name__)


@dataclass
class RegistryErgebnis:
    verbindungen: dict[str, Verbindung] = field(default_factory=dict)
    fehler: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.fehler


def lade_registry(registry_dir: Path) -> RegistryErgebnis:
    """Alle Verbindungs-YAMLs aus registry_dir laden.

    Der Ordner MUSS existieren; wenn er leer ist, ist das kein Fehler —
    dann gibt es einfach keine Verbindungen. Der User kann dann in der
    GUI eine neue anlegen.
    """
    ergebnis = RegistryErgebnis()

    if not registry_dir.exists():
        logger.warning(
            "Registry-Verzeichnis %s existiert nicht — keine Verbindungen.",
            registry_dir,
        )
        return ergebnis

    yaml_dateien = sorted(registry_dir.glob("*.yaml")) + sorted(
        registry_dir.glob("*.yml")
    )
    yaml_dateien = [p for p in yaml_dateien if not p.stem.startswith("_")]

    for datei in yaml_dateien:
        name = datei.stem
        try:
            inhalt = yaml.safe_load(datei.read_text(encoding="utf-8"))
            if not isinstance(inhalt, dict):
                ergebnis.fehler[name] = ["Datei enthaelt kein YAML-Dict"]
                continue
            v = Verbindung.model_validate(inhalt)

            if v.name != name:
                ergebnis.fehler[name] = [
                    f"Dateiname '{datei.name}' passt nicht zum 'name'-Feld "
                    f"('{v.name}') in der Datei."
                ]
                continue

            _pruefe_typ_spezifisch(v, ergebnis, name)
            if name not in ergebnis.fehler:
                ergebnis.verbindungen[name] = v

        except ValidationError as e:
            ergebnis.fehler[name] = [str(err) for err in e.errors()]
        except yaml.YAMLError as e:
            ergebnis.fehler[name] = [f"YAML-Parser-Fehler: {e}"]
        except OSError as e:
            ergebnis.fehler[name] = [f"Datei nicht lesbar: {e}"]

    logger.info(
        "Registry geladen: %d Verbindung(en), %d Fehler",
        len(ergebnis.verbindungen), len(ergebnis.fehler),
    )
    return ergebnis


def _pruefe_typ_spezifisch(
    v: Verbindung, ergebnis: RegistryErgebnis, name: str
) -> None:
    """Typ-spezifische Pflichtfelder pruefen.
    SQLite braucht `pfad`, Oracle braucht `env_prefix`.
    """
    if v.typ == "sqlite":
        if not v.pfad:
            ergebnis.fehler[name] = ["typ=sqlite braucht das Feld 'pfad'."]
    elif v.typ == "oracle":
        fehler = []
        if not v.user:
            fehler.append("typ=oracle braucht das Feld 'user'.")
        if not v.dsn:
            fehler.append("typ=oracle braucht das Feld 'dsn'.")
        if fehler:
            ergebnis.fehler[name] = fehler