"""
CLI-Einstieg fuer den Vierol-Import-Prototyp.

Aufrufe:
    python -m vierol_import run              # Alle Dateien im Ingest-Verzeichnis verarbeiten
    python -m vierol_import validate-config  # Konfigurations-Katalog gegen Meta-Schema pruefen
    python -m vierol_import watch            # Ingest-Verzeichnis dauerhaft ueberwachen
"""

from pathlib import Path

import click

from vierol_import.engine import ImportEngine
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_INGEST = Path("data/ingest")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Ausfuehrliches Logging aktivieren.")
def cli(verbose: bool) -> None:
    """Vierol Import — metadaten-gesteuerte Importarchitektur."""
    setup_logging(verbose=verbose)


@cli.command()
@click.option(
    "--catalog", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CATALOG
)
@click.option(
    "--ingest", type=click.Path(exists=True, path_type=Path), default=DEFAULT_INGEST
)
def run(catalog: Path, ingest: Path) -> None:
    """Alle Dateien im Ingest-Verzeichnis einmal verarbeiten."""
    engine = ImportEngine(catalog_dir=catalog)
    engine.process_directory(ingest)


@cli.command("validate-config")
@click.option(
    "--catalog", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CATALOG
)
def validate_config(catalog: Path) -> None:
    """Alle Konfigurationen im Katalog gegen das Meta-Schema pruefen."""
    from vierol_import.catalog.meta_schema import validate_all

    errors = validate_all(catalog)
    if errors:
        for path, error in errors:
            click.echo(f"FEHLER in {path}: {error}", err=True)
        raise click.Abort()
    click.echo(f"Alle Konfigurationen in {catalog} sind gueltig.")


@cli.command()
@click.option(
    "--catalog", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CATALOG
)
@click.option(
    "--ingest", type=click.Path(exists=True, path_type=Path), default=DEFAULT_INGEST
)
def watch(catalog: Path, ingest: Path) -> None:
    """Ingest-Verzeichnis dauerhaft ueberwachen und neue Dateien verarbeiten."""
    from vierol_import.ingest.watcher import watch_directory

    engine = ImportEngine(catalog_dir=catalog)
    watch_directory(ingest, engine)


if __name__ == "__main__":
    cli()
