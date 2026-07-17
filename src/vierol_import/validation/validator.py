"""
Validierung einer CSV-Datei gegen die Regeln aus der Konfiguration.

Der Prototyp nutzt vorzugsweise das Frictionless Framework, weil es
einen etablierten Standard fuer Tabellen-Schemas bereitstellt und
sich gut mit YAML-Konfigurationen kombinieren laesst.

Alternative: eigene Pruef-Logik mit pandas, wenn Frictionless nicht
passt. Diese Wahl im Konzept-Kapitel begruenden.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate(file_path: Path, validation_config: dict[str, Any]) -> ValidationResult:
    """
    Datei gegen die Validierungs-Konfiguration pruefen.

    TODO Frictionless anbinden. Grober Skizzenpfad:

        from frictionless import validate as fl_validate, Schema
        schema = Schema(validation_config)
        report = fl_validate(str(file_path), schema=schema)
        errors = [str(t) for t in report.tasks[0].errors]
        return ValidationResult(ok=len(errors) == 0, errors=errors)

    Beim ersten Bauen: einfach ok=True zurueckgeben, damit die Pipeline
    lauffaehig bleibt. Validierung nach und nach ausbauen.
    """
    logger.warning("validate() ist noch nicht implementiert — akzeptiert alles.")
    return ValidationResult(ok=True)
