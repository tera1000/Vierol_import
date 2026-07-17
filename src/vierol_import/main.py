"""
CLI des Vierol-Import-Prototyps.

Befehle:
    python -m vierol_import import-file <datei>   # interaktiver Import-Workflow
    python -m vierol_import run                   # Batch-Modus fuer data/ingest/
    python -m vierol_import validate-config       # Katalog gegen Meta-Schema pruefen

Diese Datei enthaelt AUSSCHLIESSLICH UI-Logik: click-Optionen, farbige
Ausgaben, User-Prompts, Verschieben von Dateien nach archive/reject.
Die eigentliche Pipeline-Orchestrierung lebt in `engine.ImportEngine`
und wird von beiden Modi (import-file, run) benutzt.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import VorschlagsRanking, klassifiziere
from vierol_import.engine import ImportEngine, Status, VerarbeitungsErgebnis
from vierol_import.ingest.watcher import (
    scanne_ingest,
    verschiebe_ins_archiv,
    verschiebe_ins_reject,
)
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_DB = Path("data/vierol_import.sqlite")
DEFAULT_INGEST = Path("data/ingest")
DEFAULT_ARCHIVE = Path("data/archive")
DEFAULT_REJECT = Path("data/reject")

logger = logging.getLogger(__name__)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Ausfuehrliches Logging aktivieren.")
def cli(verbose: bool) -> None:
    """Vierol Import — metadaten-gesteuerte Importarchitektur."""
    setup_logging(verbose=verbose)


# --- validate-config ---------------------------------------------------------


@cli.command("validate-config")
@click.option(
    "--catalog",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_CATALOG,
    show_default=True,
)
def validate_config(catalog: Path) -> None:
    """Alle Konfigurationen im Katalog gegen das Meta-Schema pruefen."""
    result = load_catalog(catalog)

    for name, cfg in sorted(result.configs.items()):
        click.secho(f"  OK      {name}", fg="green")
        click.echo(
            f"          {cfg.spalten_anzahl} Spalten, Trennzeichen "
            f"'{cfg.datei.trennzeichen}', "
            f"{'mit' if cfg.datei.hat_header else 'ohne'} Header "
            f"-> Tabelle '{cfg.zielsystem.tabelle}'"
        )

    for name, fehler in sorted(result.fehler.items()):
        click.secho(f"  FEHLER  {name}", fg="red")
        for f in fehler:
            click.echo(f"          - {f}")

    click.echo()
    if result.ok:
        click.secho(f"Katalog gueltig ({len(result.configs)} Quellen).", fg="green")
    else:
        click.secho(f"{len(result.fehler)} fehlerhafte Konfiguration(en).", fg="red")
        raise SystemExit(1)


# --- import-file (interaktiv) ------------------------------------------------


@cli.command("import-file")
@click.argument("datei", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--catalog",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_CATALOG,
    show_default=True,
)
@click.option(
    "--db",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB,
    show_default=True,
    help="Ziel-SQLite-Datenbank (wird bei Bedarf angelegt).",
)
@click.option("--quelle", default=None, help="Quelle direkt vorgeben.")
@click.option("--ja", is_flag=True, help="Besten Vorschlag ohne Rueckfrage uebernehmen.")
def import_file(
    datei: Path, catalog: Path, db: Path, quelle: str | None, ja: bool
) -> None:
    """Eine Datei interaktiv erkennen, validieren, mappen und laden."""
    result = load_catalog(catalog)
    if result.fehler:
        click.secho(
            f"Achtung: {len(result.fehler)} fehlerhafte Config(s) werden ignoriert.",
            fg="yellow",
        )
    if not result.configs:
        click.secho("Keine gueltigen Configs im Katalog.", fg="red")
        raise SystemExit(1)

    engine = ImportEngine(result.configs, db_pfad=db)

    # Quellenwahl: entweder vorgegeben oder ueber Ranking + User-Entscheidung
    if quelle is None:
        ranking = klassifiziere(datei, result.configs)
        gewaehlt = _quelle_waehlen(ranking, result.configs, auto_ja=ja)
        if gewaehlt is None:
            raise SystemExit(1)
        quelle = gewaehlt.name

    click.echo()
    click.echo(f"Verarbeite '{datei.name}' als Quelle '{quelle}' ...")
    ergebnis = engine.verarbeite_mit_quelle(datei, quelle)
    _zeige_ergebnis(ergebnis)
    if not ergebnis.erfolg:
        raise SystemExit(1)


def _quelle_waehlen(
    ranking: VorschlagsRanking,
    configs: dict[str, QuellenConfig],
    auto_ja: bool,
) -> QuellenConfig | None:
    click.echo(f"Erkennung fuer '{ranking.datei.name}':")
    kandidaten = [e for e in ranking.ergebnisse if e.moeglich]

    for i, e in enumerate(ranking.ergebnisse, start=1):
        if e.moeglich:
            balken = "#" * round(e.score * 10)
            click.echo(f"  {i}. {e.quelle:<24} [{balken:<10}] {e.score:>4.0%}")
        else:
            click.secho(f"  -  {e.quelle:<24} K.O. — {e.ko_grund}", dim=True)

    if not kandidaten:
        click.echo()
        click.secho("Keine Quelle im Katalog passt zu dieser Datei.", fg="yellow")
        click.echo(
            "-> Hier startet spaeter der Assistent zum Anlegen einer neuen Config."
        )
        return None

    bester = kandidaten[0]
    schwelle = configs[bester.quelle].klassifikation.schwellenwert
    sicher = bester.score >= schwelle

    click.echo()
    if sicher:
        click.echo(f"Vorschlag: {bester.quelle} (Score {bester.score:.0%})")
    else:
        click.secho(
            f"Kein sicherer Vorschlag (bester Score {bester.score:.0%} liegt "
            f"unter Schwellenwert {schwelle:.0%}) — bitte Quelle manuell waehlen.",
            fg="yellow",
        )

    if auto_ja:
        if sicher:
            click.echo("(--ja: Vorschlag automatisch uebernommen)")
            return configs[bester.quelle]
        click.secho("(--ja gesetzt, aber kein sicherer Vorschlag -> Abbruch)", fg="red")
        return None

    auswahl = click.prompt(
        f"Quelle uebernehmen? [Enter={1 if sicher else 'Nummer waehlen'}, "
        f"Nummer=andere Quelle, n=neue Config, a=abbrechen]",
        default="1" if sicher else "",
        show_default=False,
    ).strip().lower()

    if auswahl == "a":
        click.echo("Abgebrochen.")
        return None
    if auswahl == "n":
        click.echo("-> Assistent zum Anlegen einer neuen Config folgt.")
        return None
    if auswahl.isdigit():
        idx = int(auswahl) - 1
        if 0 <= idx < len(ranking.ergebnisse):
            gew = ranking.ergebnisse[idx]
            if not gew.moeglich:
                click.secho(
                    f"'{gew.quelle}' wurde per K.O. ausgeschlossen ({gew.ko_grund}).",
                    fg="red",
                )
                return None
            return configs[gew.quelle]
    click.secho("Ungueltige Eingabe — Abbruch.", fg="red")
    return None


# --- run (Batch-Modus) -------------------------------------------------------


@cli.command("run")
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--ingest", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_INGEST, show_default=True)
@click.option("--archive", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_ARCHIVE, show_default=True)
@click.option("--reject", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_REJECT, show_default=True)
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path),
              default=DEFAULT_DB, show_default=True)
def run(
    catalog: Path, ingest: Path, archive: Path, reject: Path, db: Path
) -> None:
    """Alle Dateien im Ingest-Verzeichnis automatisch verarbeiten."""
    result = load_catalog(catalog)
    if not result.configs:
        click.secho("Keine gueltigen Configs im Katalog — Abbruch.", fg="red")
        raise SystemExit(1)

    dateien = scanne_ingest(ingest)
    if not dateien:
        click.echo(f"Keine Dateien in {ingest}.")
        return

    engine = ImportEngine(result.configs, db_pfad=db)
    click.echo(f"Verarbeite {len(dateien)} Datei(en) aus {ingest} ...")
    click.echo()

    stats: dict[str, int] = {}
    for datei in dateien:
        click.echo(f"[{datei.name}]")
        ergebnis = engine.verarbeite_auto(datei)

        if ergebnis.quelle:
            click.echo(f"  Erkannt als '{ergebnis.quelle}' "
                       f"(Score {ergebnis.score:.0%})")

        if ergebnis.erfolg:
            verschiebe_ins_archiv(datei, archive, ergebnis.quelle)  # type: ignore[arg-type]
            click.secho(
                f"  -> geladen ({ergebnis.zeilen_geladen} Datensaetze)", fg="green"
            )
        else:
            verschiebe_ins_reject(
                datei, reject, ergebnis.fehler_grund, ergebnis.fehler_details
            )
            click.secho(f"  -> abgelehnt: {ergebnis.fehler_grund}", fg="red")

        stats[ergebnis.status.value] = stats.get(ergebnis.status.value, 0) + 1

    click.echo()
    geladen = stats.get(Status.GELADEN.value, 0)
    abgelehnt = sum(v for k, v in stats.items() if k != Status.GELADEN.value)
    click.secho(
        f"Fertig: {geladen} geladen, {abgelehnt} abgelehnt.",
        fg="green" if abgelehnt == 0 else "yellow",
    )


# --- Ergebnis-Anzeige (import-file) ------------------------------------------


def _zeige_ergebnis(e: VerarbeitungsErgebnis) -> None:
    if e.status is Status.GELADEN:
        click.secho(
            f"  OK — {e.zeilen_geladen} Datensaetze geladen "
            f"(von {e.zeilen_gesamt} validierten Zeilen).",
            fg="green",
        )
        return

    click.secho(f"  ABGELEHNT — {e.fehler_grund}:", fg="red")
    for d in e.fehler_details[:50]:
        click.echo(f"    {d}")


if __name__ == "__main__":
    cli()