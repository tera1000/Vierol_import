"""
Meta-Validierung der Konfigurationsdateien.

Prueft jede YAML-Datei im Katalog gegen ein JSON-Schema, das die
erlaubte Struktur einer Konfiguration festlegt. So werden Tippfehler
und falsche Strukturen frueh — beim `validate-config`-Aufruf — erkannt,
nicht erst zur Laufzeit.
"""

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError

META_SCHEMA_FILENAME = "_meta_schema.yaml"


def load_meta_schema(catalog_dir: Path) -> dict:
    schema_path = catalog_dir / META_SCHEMA_FILENAME
    if not schema_path.exists():
        raise FileNotFoundError(f"Meta-Schema fehlt: {schema_path}")
    with schema_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def validate_all(catalog_dir: Path) -> list[tuple[Path, str]]:
    """
    Alle Konfigurationen im Katalog gegen das Meta-Schema pruefen.

    Rueckgabe: Liste (Pfad, Fehlermeldung). Leere Liste = alles gueltig.
    """
    schema = load_meta_schema(catalog_dir)
    validator = Draft202012Validator(schema)

    errors: list[tuple[Path, str]] = []
    for path in sorted(catalog_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        for error in validator.iter_errors(data):
            errors.append((path, _format_error(error)))

    return errors


def _format_error(error: ValidationError) -> str:
    location = ".".join(str(part) for part in error.absolute_path) or "<Wurzel>"
    return f"[{location}] {error.message}"
