"""
CLI des Vierol-Import-Prototyps.

Befehle:
    python -m vierol_import import-file <datei>   # interaktiver Import-Workflow
    python -m vierol_import run                   # einheitlicher Import (file/ordner/zip/pfad)
    python -m vierol_import run-ingest            # Batch-Modus fuer data/ingest/
    python -m vierol_import validate-config       # Katalog gegen Meta-Schema pruefen
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.catalog.reader import load_catalog
from vierol_import.catalog.verbindung_registry import lade_registry
from vierol_import.classification.classifier import VorschlagsRanking, klassifiziere
from vierol_import.engine import ImportEngine, Status, VerarbeitungsErgebnis
from vierol_import.ingest.watcher import (
    scanne_ingest,
)
from vierol_import.ingest.zip_entpacker import (
    ZipEntpackfehler,
    entpacke_rekursiv,
)
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_REGISTRY = Path("zielsysteme")
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


# --- export-schema (JSON-Schema fuer VS Code) --------------------------------


@cli.command("export-schema")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("config_catalog/_schema.json"),
    show_default=True,
    help="Zielpfad fuer die JSON-Schema-Datei.",
)
def export_schema(out: Path) -> None:
    """JSON-Schema aus dem Pydantic-Meta-Schema exportieren.

    Die erzeugte Datei wird von der YAML-Extension in VS Code
    gelesen, sodass Fehler beim Schreiben einer Config direkt
    im Editor angezeigt werden (Autocomplete, Tooltips, rote Wellenlinien).
    """
    import json

    schema = QuellenConfig.model_json_schema()
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["title"] = "Vierol Import — Quellen-Konfiguration"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    click.secho(f"JSON-Schema geschrieben nach {out}", fg="green")
    click.echo("Naechster Schritt: .vscode/settings.json anpassen, damit VS Code das")
    click.echo("Schema auf 'config_catalog/*.yaml' anwendet.")


# --- gemeinsame Helper -------------------------------------------------------


def _baue_engine(
    configs, verbindungen, benutzer_modus: str = "cli",
) -> ImportEngine:
    """Engine bauen mit Registry-Verbindungen und CLI-Passwort-Provider.

    Fuer Oracle-Verbindungen wird das Passwort bei Bedarf per
    `getpass.getpass()` interaktiv abgefragt und pro Prozess-Lauf im
    Speicher gehalten. Kein Passwort auf Platte.
    """
    return ImportEngine(
        configs, verbindungen=verbindungen, benutzer_modus=benutzer_modus,
        passwort_provider=_cli_passwort_provider,
    )


# Prozess-lokaler Cache: {verbindungsname: passwort}. Lebt nur waehrend
# des CLI-Aufrufs. Nach `python -m vierol_import ...` ist alles weg.
_CLI_PW_CACHE: dict[str, str] = {}

# Prozess-lokaler Override fuer Strategie-Flags (--strategie).
# Wird nach Aufruf-Ende nicht persistiert.
_STRATEGIE_OVERRIDE: dict[str, str] = {}

# Prozess-lokaler Override fuer den Config-Namen (--quelle).
# Wenn gesetzt: Klassifikation wird umgangen und diese Config verwendet.
_QUELLE_OVERRIDE: str | None = None


def _cli_passwort_provider(verbindungsname: str) -> str:
    """Passwort per getpass abfragen, im Prozess cachen."""
    import getpass
    if verbindungsname in _CLI_PW_CACHE:
        return _CLI_PW_CACHE[verbindungsname]
    click.echo(f"\nOracle-Verbindung '{verbindungsname}' braucht ein Passwort.")
    pw = getpass.getpass("Passwort (wird nicht angezeigt): ")
    if not pw:
        raise click.Abort()
    _CLI_PW_CACHE[verbindungsname] = pw
    return pw


def _lade_katalog_und_registry(
    catalog_dir: Path, registry_dir: Path,
) -> tuple[dict, dict]:
    """Katalog + Registry laden, mit klaren Fehlermeldungen."""
    kat = load_catalog(catalog_dir)
    if not kat.configs:
        click.secho(
            f"Kein gueltiger Katalog in {catalog_dir} — Abbruch.", fg="red"
        )
        raise SystemExit(1)

    reg = lade_registry(registry_dir)
    if not reg.verbindungen:
        click.secho(
            f"Keine Zielsystem-Verbindungen in {registry_dir}. "
            f"Bitte mindestens eine YAML dort anlegen.", fg="red"
        )
        raise SystemExit(1)

    # Zusaetzlicher Check: verweisen alle Configs auf existierende Verbindungen?
    fehlende = set()
    for name, cfg in kat.configs.items():
        if cfg.zielsystem.ref not in reg.verbindungen:
            fehlende.add((name, cfg.zielsystem.ref))
    if fehlende:
        click.secho("Configs verweisen auf unbekannte Verbindungen:", fg="red")
        for cfg_name, ref in fehlende:
            click.echo(f"  {cfg_name}: ref={ref}")
        raise SystemExit(1)

    # Strategie-Override anwenden: alle Configs bekommen ggf. einen
    # anderen pk_konflikt-Wert. Der Original-YAML bleibt unangetastet;
    # die Aenderung lebt nur in dieser Prozess-Instanz.
    if _STRATEGIE_OVERRIDE:
        import copy
        neu = {}
        for name, cfg in kat.configs.items():
            neuer = copy.deepcopy(cfg)
            if "pk_konflikt" in _STRATEGIE_OVERRIDE:
                neuer.zielsystem.pk_konflikt = _STRATEGIE_OVERRIDE["pk_konflikt"]
            neu[name] = neuer
        return neu, reg.verbindungen

    return kat.configs, reg.verbindungen


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
@click.option("--archive", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_ARCHIVE, show_default=True)
@click.option("--reject", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_REJECT, show_default=True)
@click.option("--quelle", default=None, help="Quelle direkt vorgeben.")
@click.option("--ja", is_flag=True, help="Besten Vorschlag ohne Rueckfrage uebernehmen.")
@click.option("--no-move", is_flag=True,
              help="Datei NICHT ins Archiv/Reject verschieben (nur verarbeiten).")
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True,
              help="Ordner mit Zielsystem-Verbindungen (zielsysteme/*.yaml).")
def import_file(
    datei: Path, catalog: Path, db: Path, archive: Path, reject: Path,
    quelle: str | None, ja: bool, no_move: bool, registry: Path,
) -> None:
    """Eine Datei interaktiv erkennen, validieren, mappen und laden."""
    configs_geladen, verbindungen = _lade_katalog_und_registry(catalog, registry)
    engine = _baue_engine(configs_geladen, verbindungen, benutzer_modus="cli-file")
    if quelle is None:
        ranking = klassifiziere(datei, configs_geladen)
        gewaehlt = _quelle_waehlen(
            ranking, configs_geladen, auto_ja=ja, datei=datei, db_pfad=db,
        )
        if gewaehlt is None:
            raise SystemExit(1)
        quelle = gewaehlt.name

    # --- Vorbereitung: Validierung + Mapping (KEIN Schreiben) ---
    click.echo()
    click.echo(f"Verarbeite '{datei.name}' als Quelle '{quelle}' ...")
    ergebnis = engine.verarbeite_mit_quelle(datei, quelle)

    # Wenn schon die Vorbereitung fehlgeschlagen ist -> nur melden
    if not ergebnis.bereit_zum_schreiben:
        _zeige_ergebnis(ergebnis)
        raise SystemExit(1)

    # --- vorlaeufig deaktiviert: Datenvorschau ---
    # _zeige_vorschau(ergebnis)
    # --- Ende deaktiviert ---

    # --- vorlaeufig deaktiviert: y/N-Bestaetigung "In Tabelle schreiben?" ---
    # if not ja and not click.confirm(
    #     f"\nIn Tabelle '{ergebnis.cfg.zielsystem.tabelle}' schreiben?",
    #     default=False,
    # ):
    #     click.secho("Abgebrochen — nichts geschrieben, Datei bleibt liegen.", fg="yellow")
    #     raise SystemExit(0)
    # --- Ende deaktiviert ---

    # --- Schreiben ---
    click.echo("Schreibe in Zieltabelle ...")
    engine.schreibe(ergebnis)
    _zeige_ergebnis(ergebnis)

    if not ergebnis.erfolg:
        raise SystemExit(1)


def _quelle_waehlen(
    ranking: VorschlagsRanking,
    configs: dict[str, QuellenConfig],
    auto_ja: bool,
    datei: Path,
    db_pfad: Path,
) -> QuellenConfig | None:
    """Klassifikations-Ranking anzeigen und User fragen welche Quelle.

    Prompt-Optionen:
      Enter    - besten Vorschlag uebernehmen
      Nummer   - andere Quelle aus der Liste
      a        - abbrechen

    Kein 'n = neue Config' mehr (siehe Kopf-Docstring).
    """
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
        # --- vorlaeufig deaktiviert: Assistent-Hinweis fuer neue Config ---
        # click.echo("-> Hier startet spaeter der Assistent zum Anlegen einer neuen Config.")
        # --- Ende deaktiviert ---
        # Audit-Log-Eintrag bleibt, damit im Log lueckenlos alle
        # Import-Versuche stehen.
        from vierol_import.monitoring.audit_log import logge_lauf
        logge_lauf(
            db_pfad,
            dateiname=datei.name,
            status="abgelehnt_unbekannt",
            fehler_grund="Keine Config passt zu dieser Datei",
            benutzer_modus="cli-file",
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
        f"Nummer=andere Quelle, a=abbrechen]",
        default="1" if sicher else "",
        show_default=False,
    ).strip().lower()

    if auswahl == "a":
        click.echo("Abgebrochen.")
        return None
    # --- vorlaeufig deaktiviert: n = neue Config ---
    # if auswahl == "n":
    #     click.echo("-> Assistent zum Anlegen einer neuen Config folgt.")
    #     return None
    # --- Ende deaktiviert ---
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


# --- run (einheitlicher Einstiegspunkt fuer file/ordner/zip/pfad) -----------


@cli.command("run")
@click.option("--file", "einzeldatei",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Eine einzelne Datei verarbeiten.")
@click.option("--ordner", "-o", "ordner",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Alle Dateien in diesem Ordner verarbeiten.")
@click.option("--zip", "zip_datei",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="ZIP-Archiv entpacken und alle enthaltenen Dateien verarbeiten.")
@click.option("--pfad", "netzpfad",
              type=str,
              help=("Netzlaufwerk-Pfad (UNC oder gemountet), z.B. "
                    "'\\\\\\\\server\\\\share\\\\liefer'. Wird wie --ordner "
                    "behandelt, aber schreibt einen Log-Vermerk 'aus Netz'."))
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
@click.option("--archive", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_ARCHIVE, show_default=True)
@click.option("--reject", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_REJECT, show_default=True)
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path),
              default=DEFAULT_DB, show_default=True)
@click.option("--ja", is_flag=True,
              help="Alles ohne Rueckfrage — auch unsichere Zuordnungen.")
@click.option("--strategie", type=click.Choice(["append", "update", "skip"]),
              default=None,
              help="Ueberschreibt die Ladestrategie fuer diesen Lauf.")
@click.option("--quelle", "quelle_flag", type=str, default=None,
              help=("Config-Namen explizit vorgeben — Klassifikation wird "
                    "uebersprungen fuer alle Dateien im Batch."))
def run(
    einzeldatei: Path | None, ordner: Path | None, zip_datei: Path | None,
    netzpfad: str | None,
    catalog: Path, registry: Path, archive: Path, reject: Path, db: Path,
    ja: bool, strategie: str | None, quelle_flag: str | None,
) -> None:
    """Datei(en) verarbeiten. Genau eine Quellvariante angeben.

    Vier moegliche Quellen:

    \b
    --file <datei>       eine einzelne Datei
    --ordner <ordner>    alle Dateien in einem lokalen Ordner
    --zip <archiv>       alle Dateien in einem ZIP-Archiv
    --pfad <netzpfad>    Dateien auf einem Netzlaufwerk

    Verhalten:
      - Ohne --quelle wird EINMAL nach der Quelle gefragt (bei --file:
        fuer diese eine Datei; bei --ordner/--zip/--pfad: einmalig
        fuer den ganzen Batch, die gewaehlte Quelle gilt dann fuer
        alle enthaltenen Dateien).
      - Mit --quelle wird ohne Rueckfrage direkt geladen.
      - Mit --ja werden auch unsichere Zuordnungen ohne Nachfrage
        uebernommen.

    Beispiele:

    \b
      vierol_import run --file preise.csv
      vierol_import run --ordner data/ingest
      vierol_import run --zip data/lieferung.zip
      vierol_import run --file preise.csv --quelle topmotive
    """
    # Genau eine Variante muss gewaehlt sein
    varianten = [einzeldatei, ordner, zip_datei, netzpfad]
    gewaehlt = [x for x in varianten if x is not None]
    if len(gewaehlt) == 0:
        click.secho(
            "Keine Quelle angegeben. Bitte eine von "
            "--file/--ordner/--zip/--pfad waehlen.", fg="red",
        )
        raise SystemExit(1)
    if len(gewaehlt) > 1:
        click.secho(
            "Mehrere Quellen angegeben. Bitte GENAU EINE von "
            "--file/--ordner/--zip/--pfad waehlen.", fg="red",
        )
        raise SystemExit(1)

    # Strategie-Override
    if strategie is not None:
        strategie_map = {
            "append": "insert",
            "update": "update",
            "skip": "skip",
        }
        pk_wert = strategie_map[strategie]
        _STRATEGIE_OVERRIDE["pk_konflikt"] = pk_wert
        click.secho(
            f"Strategie fuer diesen Lauf: {strategie} "
            f"(pk_konflikt={pk_wert})", fg="cyan",
        )

    configs_geladen, verbindungen = _lade_katalog_und_registry(catalog, registry)

    # --- Zweig 1: einzelne Datei --------------------------------------------
    if einzeldatei is not None:
        # Delegation an bestehenden import_file — der macht die 1-Datei-
        # Frage sauber (Ranking, Prompt, Verarbeitung).
        ctx = click.get_current_context()
        ctx.invoke(
            import_file, datei=einzeldatei,
            catalog=catalog, db=db, archive=archive, reject=reject,
            quelle=quelle_flag, ja=ja, no_move=False, registry=registry,
        )
        return

    # --- Zweig 2/3/4: ordner / zip / pfad -----------------------------------
    # Ab hier: Batch-Verarbeitung mit EINER einmaligen Quellen-Frage
    # auf Batch-Ebene. Die gewaehlte Quelle wird auf ALLE enthaltenen
    # Dateien angewendet.
    engine = _baue_engine(configs_geladen, verbindungen, benutzer_modus="cli-run")

    dateien_liste, tmp_roots = _sammle_dateien_fuer_batch(
        ordner=ordner, zip_datei=zip_datei, netzpfad=netzpfad,
    )
    if not dateien_liste:
        click.secho("Keine verarbeitbaren Dateien gefunden.", fg="yellow")
        _aufraeumen_tmp(tmp_roots)
        return

    click.echo(f"{len(dateien_liste)} Datei(en) gefunden.")

    # Quelle bestimmen: entweder explizit per --quelle oder per Uebersichts-Prompt
    # (klassifiziert alle Dateien, ermittelt dominante Config)
    ergebnis_quelle = _quelle_fuer_batch_bestimmen(
        dateien_liste, configs_geladen, engine,
        quelle_flag=quelle_flag, auto_ja=ja, db_pfad=db,
    )
    if ergebnis_quelle is None:
        click.secho("Abgebrochen — keine Quelle gewaehlt.", fg="yellow")
        _aufraeumen_tmp(tmp_roots)
        raise SystemExit(1)

    quelle_name, verarbeitbare, uebersprungene = ergebnis_quelle

    # Uebersprungene Dateien loggen (kein Verarbeitungsversuch)
    if uebersprungene:
        from vierol_import.monitoring.audit_log import logge_lauf
        for datei in uebersprungene:
            logge_lauf(
                db, dateiname=datei.name,
                status="abgelehnt_unbekannt",
                fehler_grund=(
                    f"Keine Config passt (Batch-Quelle '{quelle_name}' "
                    "trifft auf diese Datei nicht zu)"
                ),
                benutzer_modus="cli-run",
            )

    # Verarbeitbare Dateien mit der gewaehlten Quelle abarbeiten
    click.echo()
    click.echo(f"Verarbeite {len(verarbeitbare)} Datei(en) "
               f"mit Quelle '{quelle_name}' ...")
    click.echo()

    # Container-Endungen: wenn eine Datei mit dieser Endung im Batch-Loop
    # landet, war das Entpacken/Erkennen vorgelagert schon fehlgeschlagen.
    # Solche Dateien sollen KEINEN Audit-Eintrag bekommen — der Audit-Log
    # dokumentiert die Verarbeitung inhaltlicher Dateien, nicht die
    # Transport-Container. Nur Konsolen-Warnung ist erwuenscht.
    CONTAINER_ENDUNGEN = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}

    stats: dict[str, int] = {}
    for datei in verarbeitbare:
        # Sicherheitsnetz: falls trotz Filterung in
        # _sammle_dateien_fuer_batch eine Container-Datei durchgerutscht
        # sein sollte, hier still uebergehen — keine Log-Ausgabe, kein
        # Audit-Eintrag. Der Anwender hat die "entpackt"-Meldung schon
        # weiter oben gesehen; die Container-Datei selbst ist nichts,
        # was verarbeitet werden koennte.
        if datei.suffix.lower() in CONTAINER_ENDUNGEN:
            continue
        click.echo(f"[{datei.name}]")
        try:
            ergebnis = engine.verarbeite_mit_quelle(datei, quelle_name)
            if ergebnis.bereit_zum_schreiben:
                engine.schreibe(ergebnis)
            _kompaktes_ergebnis(ergebnis)
            stats[ergebnis.status.value] = stats.get(ergebnis.status.value, 0) + 1
        except Exception as ex:
            # Container-Dateien (defekte/verschluesselte ZIPs etc.):
            # nur Konsole, kein Audit-Log — der Container ist keine
            # zu importierende Datei im fachlichen Sinne.
            if datei.suffix.lower() in CONTAINER_ENDUNGEN:
                click.secho(
                    f"  → uebersprungen (Container nicht verarbeitbar): "
                    f"{type(ex).__name__}", fg="yellow",
                )
                # Nicht in stats zaehlen — der Container wurde nie als
                # Datei gefuehrt, nur als moeglicher Ursprung.
                continue

            # Echte Dateien: Log-Eintrag, damit der Fehler auditierbar
            # bleibt (Datei, Zeit, Ursache).
            click.secho(
                f"  → Fehler beim Verarbeiten: "
                f"{type(ex).__name__}: {ex}", fg="red",
            )
            from vierol_import.monitoring.audit_log import logge_lauf
            logge_lauf(
                db, dateiname=datei.name,
                status="abgelehnt_ladefehler",
                fehler_grund=f"{type(ex).__name__}: {ex}",
                benutzer_modus="cli-run",
            )
            stats["abgelehnt_ladefehler"] = stats.get(
                "abgelehnt_ladefehler", 0
            ) + 1

    _aufraeumen_tmp(tmp_roots)

    click.echo()
    geladen = stats.get(Status.GELADEN.value, 0)
    abgelehnt = sum(v for k, v in stats.items() if k != Status.GELADEN.value)
    text = f"Fertig: {geladen} geladen, {abgelehnt} abgelehnt"
    if uebersprungene:
        text += f", {len(uebersprungene)} uebersprungen (keine Config passte)"
    click.secho(text + ".", fg="green" if abgelehnt == 0 else "yellow")


def _sammle_dateien_fuer_batch(
    ordner: Path | None, zip_datei: Path | None, netzpfad: str | None,
) -> tuple[list[Path], list[Path]]:
    """Fuer --ordner, --zip, --pfad die konkrete Dateiliste ermitteln.

    Rueckgabe:
      (dateien_liste, tmp_roots)
      * dateien_liste: absolute Pfade der zu verarbeitenden Dateien
      * tmp_roots: Liste temporaerer Verzeichnisse, die nach dem Lauf
                   aufgeraeumt werden muessen (bei ZIPs)
    """
    tmp_roots: list[Path] = []
    dateien: list[Path] = []

    if zip_datei is not None:
        import tempfile
        click.echo(f"Entpacke '{zip_datei.name}' ...")
        tmp_root = Path(tempfile.mkdtemp(prefix="vierol_zip_"))
        tmp_roots.append(tmp_root)
        entpack_dir = tmp_root / zip_datei.stem
        try:
            entpackt = entpacke_rekursiv(zip_datei, entpack_dir)
            dateien = [e.pfad for e in entpackt]
        except ZipEntpackfehler as e:
            click.secho(f"Fehler beim Entpacken: {e}", fg="red")
            return [], tmp_roots
        return dateien, tmp_roots

    if ordner is not None or netzpfad is not None:
        basis = ordner if ordner is not None else Path(netzpfad)  # type: ignore
        if netzpfad is not None:
            click.secho(f"Netzpfad: {netzpfad}", fg="cyan")
            if not basis.exists():
                click.secho(f"Netzpfad nicht erreichbar: {netzpfad}", fg="red")
                return [], tmp_roots
            if not basis.is_dir():
                click.secho(f"Netzpfad ist kein Verzeichnis: {netzpfad}", fg="red")
                return [], tmp_roots
        rohe_dateien = scanne_ingest(basis)

        # ZIP-Dateien im Ordner rekursiv entpacken
        import tempfile
        zip_dateien = [d for d in rohe_dateien if _ist_zip_datei(d)]
        normale_dateien = [d for d in rohe_dateien if not _ist_zip_datei(d)]
        zusaetzliche: list[Path] = []

        if zip_dateien:
            ordner_kontext = basis.name or "ingest"
            for zd in zip_dateien:
                tmp_root = Path(tempfile.mkdtemp(prefix="vierol_zip_"))
                tmp_roots.append(tmp_root)
                entpack_dir = tmp_root / ordner_kontext / zd.stem
                try:
                    entpackt = entpacke_rekursiv(zd, entpack_dir)
                    zusaetzliche.extend(e.pfad for e in entpackt)
                    click.secho(
                        f"[{zd.name}] {len(entpackt)} Datei(en) entpackt.",
                        fg="cyan",
                    )
                except ZipEntpackfehler as e:
                    click.secho(f"[{zd.name}] Entpacken fehlgeschlagen: {e}",
                                fg="red")

        dateien = normale_dateien + zusaetzliche

    # Sicherheitsnetz: Container-Dateien (.zip, .rar etc.) sollen nie in
    # der Verarbeitungsliste landen. Sie sind Transport-Container, keine
    # inhaltlichen Datenlieferungen. Falls entpacke_rekursiv eine innere
    # .zip nicht mit-entpackt hat, oder falls ein .zip aus anderen
    # Gruenden in `dateien` gelandet ist, filtern wir sie hier raus —
    # der Anwender hat die "entpackt"-Meldung oben schon gesehen, mehr
    # Log-Rauschen zur .zip-Datei ist nicht erwuenscht.
    _container_endungen = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
    dateien = [
        d for d in dateien
        if d.suffix.lower() not in _container_endungen
    ]

    return dateien, tmp_roots


def _aufraeumen_tmp(tmp_roots: list[Path]) -> None:
    """Temporaere Verzeichnisse aufraeumen."""
    import shutil
    for r in tmp_roots:
        shutil.rmtree(r, ignore_errors=True)


def _quelle_fuer_batch_bestimmen(
    dateien_liste: list[Path], configs: dict[str, QuellenConfig],
    engine: ImportEngine, quelle_flag: str | None, auto_ja: bool,
    db_pfad: Path,
) -> tuple[str, list[Path], list[Path]] | None:
    """Ermittelt die Quelle, die auf den Batch angewendet wird.

    Klassifiziert ALLE Dateien im Batch (nicht nur die erste), ermittelt
    die dominierende Config und bietet sie als Vorschlag an. Dateien, zu
    denen keine Config passt (z.B. mitgelieferte Beschreibungsdateien
    wie 'PVG_record-definition.txt') werden aussortiert.

    Rueckgabe:
      (quelle_name, verarbeitbare_dateien, uebersprungene_dateien)
      oder None bei Abbruch.
    """
    from collections import Counter

    # Explizit vorgegeben? Dann ALLE Dateien mit dieser Quelle versuchen —
    # der User hat explizit entschieden, wir vertrauen dem.
    if quelle_flag is not None:
        if quelle_flag not in configs:
            click.secho(
                f"Config '{quelle_flag}' nicht im Katalog. Verfuegbar: "
                f"{sorted(configs)}", fg="red",
            )
            return None
        click.secho(f"Explizite Quelle: {quelle_flag}", fg="cyan")
        return (quelle_flag, dateien_liste, [])

    # Sonst: alle Dateien klassifizieren
    click.echo()
    click.echo(f"Klassifiziere {len(dateien_liste)} Datei(en) ...")

    # Pro Datei: welche Configs passen ueber Schwelle?
    passende: list[tuple[Path, VorschlagsRanking, list[str]]] = []
    unpassende: list[Path] = []
    for datei in dateien_liste:
        ranking = klassifiziere(datei, configs)
        matches = []
        for e in ranking.ergebnisse:
            if not e.moeglich:
                continue
            schwelle = configs[e.quelle].klassifikation.schwellenwert
            if e.score >= schwelle:
                matches.append(e.quelle)
        if matches:
            passende.append((datei, ranking, matches))
        else:
            unpassende.append(datei)

    # Kein einziges Match — Abbruch
    if not passende:
        click.secho(
            "Keine Datei im Batch passt zu einer Config.", fg="red",
        )
        for datei in unpassende:
            click.echo(f"  - {datei.name}")
        # Audit-Log-Eintrag pro nicht importierbarer Datei
        from vierol_import.monitoring.audit_log import logge_lauf
        for datei in unpassende:
            logge_lauf(
                db_pfad, dateiname=datei.name,
                status="abgelehnt_unbekannt",
                fehler_grund="Keine Config passt zu dieser Datei",
                benutzer_modus="cli-run",
            )
        return None

    # Passende Configs zaehlen: pro Datei ALLE Configs, die matchen
    # (nicht nur die "beste") — sonst versteckt man dem User Alternativen
    # bei Score-Gleichstand. Wenn eine Datei mit 100% zu drei Configs
    # passt, soll er alle drei zur Auswahl haben.
    config_counter: Counter[str] = Counter()
    for datei, ranking, matches in passende:
        for cfg_name in matches:
            config_counter[cfg_name] += 1

    # Uebersicht ausgeben
    click.echo()
    click.echo(
        f"Klassifikations-Uebersicht ({len(dateien_liste)} Datei(en)):"
    )
    click.echo(
        "  Passende Zuordnungen (eine Datei kann zu mehreren Configs passen):"
    )
    for cfg_name, cnt in config_counter.most_common():
        click.secho(f"    {cnt:>3} Datei(en) → {cfg_name}", fg="green")
    if unpassende:
        click.secho(
            f"  {len(unpassende):>3} nicht importierbar "
            "(keine Config passt)",
            fg="yellow",
        )
        for datei in unpassende:
            click.echo(f"       - {datei.name}")

    # Sortierte Liste der Config-Vorschlaege (haeufigste zuerst)
    verfuegbare = [cfg_name for cfg_name, _ in config_counter.most_common()]
    dominant = verfuegbare[0]

    click.echo()
    if len(verfuegbare) == 1:
        click.echo(f"Vorschlag: {dominant} "
                   f"(auf {config_counter[dominant]} Datei(en) anwendbar)")
    else:
        click.echo("Verfuegbare Quellen fuer diesen Batch:")
        for i, cfg_name in enumerate(verfuegbare, 1):
            click.echo(f"  {i}. {cfg_name} "
                       f"({config_counter[cfg_name]} Datei(en))")
        click.echo(f"Vorschlag: {dominant}")

    # --ja: nimm dominante Config
    if auto_ja:
        click.echo("(--ja: Vorschlag automatisch uebernommen)")
        gefiltert = [
            d for d, _, matches in passende if dominant in matches
        ]
        uebersprungen = [d for d in dateien_liste if d not in gefiltert]
        return (dominant, gefiltert, uebersprungen)

    # Interaktiv
    auswahl = click.prompt(
        "Quelle uebernehmen? [Enter=Vorschlag, Nummer=andere Quelle, a=abbrechen]",
        default="1",
        show_default=False,
    ).strip().lower()

    if auswahl == "a":
        return None

    if auswahl == "" or auswahl == "1":
        gewaehlt = dominant
    elif auswahl.isdigit():
        idx = int(auswahl) - 1
        if 0 <= idx < len(verfuegbare):
            gewaehlt = verfuegbare[idx]
        else:
            click.secho("Ungueltige Nummer.", fg="red")
            return None
    else:
        click.secho("Ungueltige Eingabe.", fg="red")
        return None

    # Dateien filtern: nur die verarbeiten, die zur gewaehlten Config passen
    gefiltert = [
        d for d, _, matches in passende if gewaehlt in matches
    ]
    uebersprungen = [d for d in dateien_liste if d not in gefiltert]

    if not gefiltert:
        # sollte nicht passieren, aber sicherheitshalber
        click.secho(f"Keine Datei passt zu '{gewaehlt}'.", fg="red")
        return None

    click.echo(
        f"→ Quelle '{gewaehlt}' wird auf {len(gefiltert)} Datei(en) angewendet, "
        f"{len(uebersprungen)} werden uebersprungen."
    )
    return (gewaehlt, gefiltert, uebersprungen)


def _kompaktes_ergebnis(e: VerarbeitungsErgebnis) -> None:
    """Einzeilige Ergebnismeldung pro Datei fuer den Batch-Modus."""
    if e.erfolg:
        text = f"  → geladen ({e.zeilen_geladen} neu"
        if e.zeilen_uebersprungen:
            text += f", {e.zeilen_uebersprungen} uebersprungen"
        if e.zeilen_quarantaene:
            text += f", {e.zeilen_quarantaene} Quarantaene"
        text += ")"
        click.secho(text, fg="green")
    else:
        click.secho(f"  → abgelehnt: {e.fehler_grund}", fg="red")


# --- run-ingest (Legacy-Alias: Ordner data/ingest verarbeiten) --------------


@cli.command("run-ingest")
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
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True,
              help="Ordner mit Zielsystem-Verbindungen (zielsysteme/*.yaml).")
@click.option("--ja", is_flag=True, help="Ohne Rueckfrage.")
@click.option("--quelle", "quelle_flag", type=str, default=None,
              help="Config-Namen explizit vorgeben.")
def run_ingest(
    catalog: Path, ingest: Path, archive: Path, reject: Path, db: Path,
    registry: Path, ja: bool, quelle_flag: str | None,
) -> None:
    """Alle Dateien im Ingest-Verzeichnis verarbeiten (Alias fuer run --ordner)."""
    # Direkte Delegation an run --ordner
    ctx = click.get_current_context()
    ctx.invoke(
        run, einzeldatei=None, ordner=ingest, zip_datei=None, netzpfad=None,
        catalog=catalog, registry=registry, archive=archive, reject=reject,
        db=db, ja=ja, strategie=None, quelle_flag=quelle_flag,
    )


def _ist_zip_datei(pfad: Path) -> bool:
    """ZIP-Erkennung: erst Dateiendung, dann Magic-Bytes als Ergaenzung.

    Warum die Endung Vorrang hat: der Magic-Bytes-Check muss die Datei
    binaer oeffnen. Bei kurzen Datei-Locks (Windows-Antivirus etc.) oder
    fehlenden Berechtigungen wirft `open()` einen OSError, und die Datei
    wuerde faelschlicherweise als "keine ZIP" gelten. Anschliessend
    versucht der Validator sie als Text zu lesen und crasht.

    Fix: jede `.zip`-Datei wird als ZIP-Kandidat behandelt. Ist sie
    korrupt, wirft der Entpacker eine klare Meldung und die Datei wird
    uebersprungen — nicht das ganze Programm gekippt.
    """
    if pfad.suffix.lower() == ".zip":
        return True
    # Fallback: Magic-Bytes fuer ZIPs ohne .zip-Endung (selten)
    try:
        with open(pfad, "rb") as f:
            head = f.read(4)
        return head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06")
    except OSError:
        return False


# --- import-zip (Legacy-Alias: ZIP entpacken und verarbeiten) --------------


@cli.command("import-zip")
@click.argument("zip_datei", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--archive", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_ARCHIVE, show_default=True)
@click.option("--reject", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_REJECT, show_default=True)
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path),
              default=DEFAULT_DB, show_default=True)
@click.option("--ja", is_flag=True, help="Ohne Rueckfrage.")
@click.option("--quelle", "quelle_flag", type=str, default=None,
              help="Config-Namen explizit vorgeben.")
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
def import_zip(
    zip_datei: Path, catalog: Path, archive: Path, reject: Path,
    db: Path, ja: bool, quelle_flag: str | None, registry: Path,
) -> None:
    """ZIP entpacken und verarbeiten (Alias fuer run --zip)."""
    ctx = click.get_current_context()
    ctx.invoke(
        run, einzeldatei=None, ordner=None, zip_datei=zip_datei,
        netzpfad=None,
        catalog=catalog, registry=registry, archive=archive, reject=reject,
        db=db, ja=ja, strategie=None, quelle_flag=quelle_flag,
    )


# --- zeige-log (letzte N Eintraege aus der Audit-Tabelle) -------------------


@cli.command("zeige-log")
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path),
              default=DEFAULT_DB, show_default=True)
@click.option("-n", "--anzahl", type=int, default=20, show_default=True,
              help="Wie viele der letzten Eintraege anzeigen.")
def zeige_log(db: Path, anzahl: int) -> None:
    """Die letzten Import-Vorgaenge aus der Audit-Tabelle anzeigen."""
    from vierol_import.monitoring.audit_log import hole_letzte

    eintraege = hole_letzte(db, anzahl=anzahl)
    if not eintraege:
        click.echo("Noch keine Import-Vorgaenge im Log.")
        return

    click.echo(
        f"{'ID':>5} {'Zeit':<20} {'Datei':<32} {'Quelle':<20} "
        f"{'Status':<25} {'geladen':>8} {'quaran':>8}"
    )
    click.echo("-" * 124)
    for e in eintraege:
        lauf_id = e.get("id") or 0
        zeit = (e.get("zeitstempel") or "")[:19]
        datei = (e.get("dateiname") or "")[:31]
        quelle = (e.get("quelle") or "-")[:19]
        status = (e.get("status") or "-")[:24]
        geladen = e.get("zeilen_geladen") or 0
        quaran = e.get("zeilen_quarantaene") or 0
        farbe = "green" if e.get("status") == "geladen" else "yellow"
        click.secho(
            f"{lauf_id:>5} {zeit:<20} {datei:<32} {quelle:<20} "
            f"{status:<25} {geladen:>8} {quaran:>8}",
            fg=farbe,
        )
    click.echo()
    click.echo(
        "Details zu Fehlern eines Laufs anzeigen: "
        "vierol_import zeige-fehler --lauf <ID>"
    )


@cli.command("zeige-fehler")
@click.option("--db", type=click.Path(dir_okay=False, path_type=Path),
              default=DEFAULT_DB, show_default=True)
@click.option("--lauf", "lauf_id", type=int, default=None,
              help="Lauf-ID aus 'zeige-log'. Ohne Angabe: Fehler des letzten Laufs.")
@click.option("-n", "--anzahl", type=int, default=50, show_default=True,
              help="Maximale Anzahl der angezeigten Fehler.")
def zeige_fehler(db: Path, lauf_id: int | None, anzahl: int) -> None:
    """Die einzelnen Fehler-Zeilen eines Import-Laufs anzeigen."""
    import sqlite3
    if not db.exists():
        click.echo(f"Keine Audit-DB unter {db}.")
        return

    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        if lauf_id is None:
            zeile = con.execute(
                "SELECT id, dateiname FROM import_lauf "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if zeile is None:
                click.echo("Noch keine Import-Vorgaenge im Log.")
                return
            lauf_id = zeile["id"]
            click.echo(f"(Letzter Lauf: #{lauf_id} — {zeile['dateiname']})")

        kopf = con.execute(
            "SELECT * FROM import_lauf WHERE id = ?", (lauf_id,),
        ).fetchone()
        if kopf is None:
            click.secho(f"Lauf #{lauf_id} nicht gefunden.", fg="red")
            return

        click.echo()
        click.secho(
            f"Lauf #{lauf_id}: {kopf['dateiname']} "
            f"({kopf['quelle'] or '-'})", bold=True,
        )
        click.echo(
            f"  Status: {kopf['status']}   "
            f"Zeilen: {kopf['zeilen_gesamt']} gesamt, "
            f"{kopf['zeilen_geladen']} geladen, "
            f"{kopf['zeilen_quarantaene']} Quarantaene"
        )
        if kopf["fehler_grund"]:
            click.echo(f"  Grund:  {kopf['fehler_grund']}")

        fehler = con.execute(
            "SELECT zeilennummer, spalte, wert, roh_zeile, grund "
            "FROM import_fehler WHERE lauf_id = ? "
            "ORDER BY zeilennummer LIMIT ?",
            (lauf_id, anzahl),
        ).fetchall()

        gesamt = con.execute(
            "SELECT COUNT(*) FROM import_fehler WHERE lauf_id = ?",
            (lauf_id,),
        ).fetchone()[0]

        if not fehler:
            click.echo()
            click.secho("  Keine Detail-Fehler gespeichert.", fg="yellow")
            return

        click.echo()
        click.echo(f"  Fehler-Details ({len(fehler)} von {gesamt} angezeigt):")
        click.echo(
            f"    {'Zeile':>7} | {'Spalte':<20} | {'Wert':<25} | Grund"
        )
        click.echo("    " + "-" * 100)
        for f in fehler:
            zn = str(f["zeilennummer"]) if f["zeilennummer"] else "-"
            spalte = (f["spalte"] or "-")[:20]
            wert = (f["wert"] or "")[:25]
            grund = (f["grund"] or "")[:60]
            click.echo(
                f"    {zn:>7} | {spalte:<20} | {wert:<25} | {grund}"
            )
            # Roh-Zeile — nur anzeigen wenn vorhanden (Datenminimierung
            # kann sie in der Config abgeschaltet haben)
            roh = f["roh_zeile"]
            if roh:
                # Auf eine Konsolen-Breite kuerzen, damit die Tabelle
                # lesbar bleibt
                roh_kurz = roh[:180] + ("..." if len(roh) > 180 else "")
                click.secho(f"             Rohzeile: {roh_kurz}", dim=True)


# --- Ergebnis-Anzeige (import-file) ------------------------------------------


def _zeige_vorschau(e: VerarbeitungsErgebnis, max_zeilen: int = 5) -> None:
    """Vorschau des Mapping-Ergebnisses (fuer den Fall dass wieder aktiviert wird).

    Aktuell wird diese Funktion aus keinem Ausfuehrungspfad mehr aufgerufen —
    die Aufrufer haben den Aufruf auskommentiert. Die Funktion bleibt als
    schneller Wiedereinstieg im Code stehen, falls die Vorschau wieder
    gebraucht wird (z.B. per neuem --vorschau-Flag).
    """
    assert e.mapping is not None and e.cfg is not None

    click.echo()
    click.secho(
        f"  Vorschau: {e.zeilen_gesamt} Datensaetze -> "
        f"Tabelle '{e.cfg.zielsystem.tabelle}' "
        f"(pk_konflikt: {e.cfg.zielsystem.pk_konflikt})",
        fg="cyan",
    )
    click.echo()

    zielfelder = e.mapping.zielfelder
    saetze = e.mapping.saetze[:max_zeilen]

    breiten = {f: max(len(f), max((len(str(s.get(f))) for s in saetze), default=0))
               for f in zielfelder}

    header = " | ".join(f.ljust(breiten[f]) for f in zielfelder)
    trenner = "-+-".join("-" * breiten[f] for f in zielfelder)
    click.echo("  " + header)
    click.echo("  " + trenner)
    for satz in saetze:
        zeile = " | ".join(str(satz.get(f) if satz.get(f) is not None else "")
                          .ljust(breiten[f]) for f in zielfelder)
        click.echo("  " + zeile)

    wenigere = len(e.mapping.saetze) - len(saetze)
    if wenigere > 0:
        click.echo(f"  ... und {wenigere} weitere Datensaetze")


def _zeige_ergebnis(e: VerarbeitungsErgebnis) -> None:
    if e.status is Status.GELADEN:
        text = (
            f"  OK — {e.zeilen_geladen} Datensaetze geladen "
            f"(von {e.zeilen_gesamt} Zeilen)"
        )
        if e.zeilen_uebersprungen:
            text += f", {e.zeilen_uebersprungen} uebersprungen (PK existierte)"
        if e.zeilen_quarantaene:
            text += f", {e.zeilen_quarantaene} in Quarantaene (partieller Modus)"
        click.secho(text + ".", fg="green")
        return

    click.secho(f"  ABGELEHNT — {e.fehler_grund}:", fg="red")
    for d in e.fehler_details[:50]:
        click.echo(f"    {d}")


# --- inspect: Struktur- und Wertevorschau (nicht-interaktiv) -----------------


@cli.command("inspect")
@click.argument("datei", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-n", "--zeilen", type=int, default=10, show_default=True,
              help="Wieviele Datenzeilen zeigen.")
def inspect_datei(datei: Path, zeilen: int) -> None:
    """Datei anschauen ohne sie zu verarbeiten."""
    from vierol_import.encoding_erkennung import erkenne_encoding

    click.echo(f"\n=== inspect: {datei.name} ===\n")
    click.echo(f"Pfad:    {datei}")
    click.echo(f"Groesse: {_menschlich(datei.stat().st_size)}")

    encoding = erkenne_encoding(datei)
    click.echo(f"Encoding: {encoding}")

    trenn = _raten_trennzeichen(datei, encoding)
    click.echo(f"Vermutetes Trennzeichen: {repr(trenn)}")

    zeilen_gesamt = _zaehle_zeilen(datei, encoding)
    click.echo(f"Zeilen (inkl. Header falls vorhanden): {zeilen_gesamt}")

    click.echo(f"\n--- Erste {zeilen} Zeilen ---")
    try:
        with open(datei, "r", encoding=encoding, newline="") as f:
            for i, zeile in enumerate(f):
                if i >= zeilen:
                    break
                felder = zeile.rstrip("\n\r").split(trenn)
                click.echo(f"  [{i+1}] ({len(felder)} Feld(er)): {zeile.rstrip()}")
    except UnicodeDecodeError as e:
        click.secho(f"Encoding-Fehler beim Lesen: {e}", fg="red")

    click.echo("\nHinweise:")
    click.echo("  - Konfiguration: config_catalog/<name>.yaml anlegen")
    click.echo(f"  - Setze trennzeichen: {repr(trenn)} und encoding: {encoding}")
    click.echo("  - Danach 'vierol_import dry-run --config <name> --file "
               f"{datei}' zum Testen")


def _menschlich(bytes_: int) -> str:
    for einheit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {einheit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} TB"


def _raten_trennzeichen(datei: Path, encoding: str) -> str:
    kandidaten = [";", ",", "|", "\t"]
    try:
        with open(datei, "r", encoding=encoding, newline="") as f:
            probe = f.readline().rstrip("\r\n")
    except (OSError, UnicodeDecodeError):
        return ";"
    if not probe:
        return ";"
    ergebnisse = [(c, len(probe.split(c))) for c in kandidaten]
    sieger = max(ergebnisse, key=lambda x: x[1])
    return sieger[0] if sieger[1] > 1 else ";"


def _zaehle_zeilen(datei: Path, encoding: str, max_zeilen: int = 1_000_000) -> int:
    count = 0
    try:
        with open(datei, "r", encoding=encoding, newline="") as f:
            for _ in f:
                count += 1
                if count >= max_zeilen:
                    return count
    except (OSError, UnicodeDecodeError):
        return -1
    return count


# --- list-connections -------------------------------------------------------


@cli.command("list-connections")
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
def list_connections(registry: Path) -> None:
    """Alle registrierten Zielsystem-Verbindungen auflisten."""
    reg = lade_registry(registry)
    if not reg.verbindungen:
        click.echo(f"Keine Verbindungen im Ordner {registry}.")
        return

    click.echo(f"\n=== Verbindungen ({len(reg.verbindungen)}) ===\n")
    for name, v in sorted(reg.verbindungen.items()):
        click.secho(f"  {name}", fg="cyan", bold=True)
        if v.beschreibung:
            click.echo(f"    Beschreibung: {v.beschreibung}")
        click.echo(f"    Typ:  {v.typ}")
        if v.typ == "sqlite":
            click.echo(f"    Pfad: {v.pfad}")
        else:
            click.echo(f"    User: {v.user}")
            click.echo(f"    DSN:  {v.dsn}")
            if v.schema_:
                click.echo(f"    Schema: {v.schema_}")
        click.echo()

    if reg.fehler:
        click.secho(f"\n{len(reg.fehler)} fehlerhafte Verbindungs-YAML(s):",
                    fg="yellow")
        for name, meldungen in reg.fehler.items():
            click.echo(f"  {name}:")
            for m in meldungen[:3]:
                click.echo(f"    - {m}")


# --- connection-test --------------------------------------------------------


@cli.command("connection-test")
@click.argument("name", type=str)
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
def connection_test(name: str, registry: Path) -> None:
    """Eine Verbindung testweise aufbauen und ein SELECT 1 absetzen."""
    reg = lade_registry(registry)
    v = reg.verbindungen.get(name)
    if v is None:
        click.secho(f"Verbindung '{name}' nicht gefunden. Verfuegbar:", fg="red")
        for x in sorted(reg.verbindungen):
            click.echo(f"  - {x}")
        raise SystemExit(1)

    click.echo(f"Teste Verbindung '{name}' ({v.typ}) ...")

    if v.typ == "sqlite":
        try:
            import sqlite3
            pfad = Path(v.pfad)
            pfad.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(pfad)
            con.execute("SELECT 1").fetchone()
            con.close()
            click.secho(f"  OK — SQLite unter {pfad} erreichbar.", fg="green")
        except Exception as e:
            click.secho(f"  FEHLER — {type(e).__name__}: {e}", fg="red")
            raise SystemExit(1)
        return

    if v.typ == "oracle":
        import getpass
        click.echo(f"  User: {v.user}")
        click.echo(f"  DSN:  {v.dsn}")
        passwort = getpass.getpass("  Passwort (wird nicht angezeigt): ")
        if not passwort:
            click.secho("  Kein Passwort eingegeben — Abbruch.", fg="yellow")
            raise SystemExit(1)
        try:
            import oracledb
        except ImportError:
            click.secho("  FEHLER — 'oracledb' nicht installiert.", fg="red")
            raise SystemExit(1)
        try:
            con = oracledb.connect(user=v.user, password=passwort, dsn=v.dsn)
            con.cursor().execute("SELECT 1 FROM dual").fetchone()
            con.close()
            click.secho("  OK — Oracle-Verbindung funktioniert.", fg="green")
        except Exception as e:
            click.secho(f"  FEHLER — {type(e).__name__}: {e}", fg="red")
            raise SystemExit(1)


# --- connection-delete ------------------------------------------------------


@cli.command("connection-delete")
@click.argument("name", type=str)
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
@click.option("--ja", is_flag=True, help="Loeschen ohne Rueckfrage bestaetigen.")
def connection_delete(
    name: str, catalog: Path, registry: Path, ja: bool,
) -> None:
    """Eine Verbindung loeschen (mit Referenzcheck)."""
    reg = lade_registry(registry)
    if name not in reg.verbindungen:
        click.secho(f"Verbindung '{name}' nicht gefunden.", fg="red")
        raise SystemExit(1)

    kat = load_catalog(catalog)
    referenzen = [
        cfg_name for cfg_name, cfg in kat.configs.items()
        if cfg.zielsystem.ref == name
    ]
    if referenzen:
        click.secho(
            f"Verbindung '{name}' wird von {len(referenzen)} Config(s) benutzt:",
            fg="yellow",
        )
        for r in referenzen:
            click.echo(f"  - {r}")
        click.echo(
            "\nBitte die Configs erst umbiegen oder loeschen, bevor die "
            "Verbindung entfernt wird."
        )
        raise SystemExit(1)

    ziel = registry / f"{name}.yaml"
    if not ja:
        click.echo(f"Verbindung '{name}' loeschen? Datei {ziel} wird entfernt.")
        if not click.confirm("Weiter?"):
            click.echo("Abgebrochen.")
            return

    ziel.unlink()
    click.secho(f"OK — Verbindung '{name}' geloescht.", fg="green")


# --- list-configs -----------------------------------------------------------


@cli.command("list-configs")
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
def list_configs(catalog: Path, registry: Path) -> None:
    """Alle Quellen-Configs anzeigen — mit Nutzungsstatistik."""
    import sqlite3
    from collections import defaultdict

    kat = load_catalog(catalog)
    reg = lade_registry(registry)

    stats = defaultdict(
        lambda: {"laeufe": 0, "zeilen": 0, "letzter": None},
    )
    audit_pfad = None
    for v in reg.verbindungen.values():
        if v.typ == "sqlite" and v.pfad and Path(v.pfad).exists():
            audit_pfad = Path(v.pfad)
            break

    if audit_pfad:
        try:
            with sqlite3.connect(audit_pfad) as con:
                rows = con.execute(
                    """
                    SELECT quelle,
                           COUNT(*),
                           COALESCE(SUM(zeilen_geladen), 0),
                           MAX(zeitstempel)
                      FROM import_lauf
                     WHERE status = 'geladen'
                     GROUP BY quelle
                    """
                ).fetchall()
                for quelle, laeufe, zeilen, letzter in rows:
                    stats[quelle] = {
                        "laeufe": laeufe, "zeilen": zeilen or 0,
                        "letzter": letzter,
                    }
        except sqlite3.OperationalError:
            pass

    if not kat.configs:
        click.echo("Keine Configs im Katalog.")
        return

    click.echo(f"\n=== Quellen-Configs ({len(kat.configs)}) ===\n")
    for name, cfg in sorted(kat.configs.items()):
        s = stats.get(name, {"laeufe": 0, "zeilen": 0, "letzter": None})
        farbe = "green" if s["laeufe"] > 0 else "white"
        click.secho(f"  {name}", fg=farbe, bold=True)
        click.echo(
            f"    -> {cfg.zielsystem.ref}.{cfg.zielsystem.tabelle} "
            f"({len(cfg.spalten)} Spalten, {repr(cfg.datei.trennzeichen)})"
        )
        if s["laeufe"] > 0:
            click.echo(
                f"    Laeufe: {s['laeufe']}  Zeilen geladen: {s['zeilen']}  "
                f"Letzter: {s['letzter']}"
            )
        else:
            click.echo("    Laeufe: — (noch nie erfolgreich geladen)")
        click.echo()

    if kat.fehler:
        click.secho(f"\n{len(kat.fehler)} fehlerhafte Config-YAML(s):",
                    fg="yellow")
        for name in kat.fehler:
            click.echo(f"  - {name}")


# --- dry-run ---------------------------------------------------------------


@cli.command("dry-run")
@click.option("--config", "config_name", type=str, required=True,
              help="Name der Quellen-Config im Katalog.")
@click.option("--file", "datei", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="Beispieldatei zum Testen.")
@click.option("--catalog", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_CATALOG, show_default=True)
@click.option("--registry", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_REGISTRY, show_default=True)
@click.option("-n", "--zeilen-vorschau", type=int, default=5, show_default=True,
              help="Wieviele gemappte Zeilen zeigen.")
def dry_run(
    config_name: str, datei: Path, catalog: Path, registry: Path,
    zeilen_vorschau: int,
) -> None:
    """Volle Pipeline (Klassifikation, Validierung, Mapping) OHNE DB-Load."""
    from vierol_import.classification.classifier import bewerte_datei
    from vierol_import.validation.validator import validiere
    from vierol_import.mapping.mapper import mappe

    configs_geladen, verbindungen = _lade_katalog_und_registry(catalog, registry)
    if config_name not in configs_geladen:
        click.secho(f"Config '{config_name}' nicht gefunden. Verfuegbar:", fg="red")
        for x in sorted(configs_geladen):
            click.echo(f"  - {x}")
        raise SystemExit(1)

    cfg = configs_geladen[config_name]
    click.echo(f"\n=== dry-run: {config_name} auf {datei.name} ===\n")

    click.echo("Stufe 1: Klassifikation")
    kl = bewerte_datei(datei, cfg)
    if kl.moeglich:
        click.secho(f"  OK — Score {kl.score:.0%}", fg="green")
    else:
        click.secho(f"  K.O. — {kl.ko_grund}", fg="red")
        click.echo("  (dry-run zeigt trotzdem was folgen wuerde)")

    click.echo("\nStufe 2: Validierung")
    val = validiere(datei, cfg)
    zeilen_ok = val.zeilen_gesamt - val.zeilen_fehlerhaft
    if val.ok:
        click.secho(f"  OK — {zeilen_ok} gueltige Zeile(n).", fg="green")
    else:
        click.secho(
            f"  Probleme — {zeilen_ok} ok, {val.zeilen_fehlerhaft} fehlerhaft.",
            fg="yellow",
        )
        for f in val.fehler[:5]:
            click.echo(f"    - Zeile {f.zeile}: {f.grund}")
        if len(val.fehler) > 5:
            click.echo(f"    ... und {len(val.fehler) - 5} weitere")

    if zeilen_ok == 0:
        click.secho("\n  KEIN Mapping moeglich — keine gueltigen Zeilen.", fg="red")
        return

    click.echo("\nStufe 3: Mapping (kanonisches Modell)")
    gute = val.gute_zeilen if val.gute_zeilen else set(range(
        1 if not cfg.datei.hat_header else 2,
        val.zeilen_gesamt + (1 if not cfg.datei.hat_header else 2),
    ))
    ergebnis = mappe(datei, cfg, nur_zeilen=gute)
    click.secho(
        f"  OK — {len(ergebnis.saetze)} Datensatz(e), "
        f"{len(ergebnis.zielfelder)} Zielfeld(er)", fg="green",
    )

    click.echo(f"\n  Erste {min(zeilen_vorschau, len(ergebnis.saetze))} gemappte Zeile(n):")
    click.echo(f"  Felder: {ergebnis.zielfelder}")
    for i, satz in enumerate(ergebnis.saetze[:zeilen_vorschau]):
        werte = [str(satz.get(f)) for f in ergebnis.zielfelder]
        click.echo(f"    [{i+1}] {werte}")

    click.echo("\nStufe 4: Load-Vorschau (NICHT ausgefuehrt)")
    v = verbindungen.get(cfg.zielsystem.ref)
    if v is None:
        click.secho(f"  Verbindung '{cfg.zielsystem.ref}' nicht in Registry.", fg="red")
        return
    if v.typ == "sqlite":
        click.echo(f"  Wuerde schreiben nach: {v.pfad} :: {cfg.zielsystem.tabelle}")
    else:
        click.echo(f"  Wuerde schreiben nach: {v.user}@{v.dsn} :: "
                   f"{cfg.zielsystem.tabelle}")
    click.echo(f"  PK-Konflikt-Modus: {cfg.zielsystem.pk_konflikt}")
    if cfg.zielsystem.upsert_key:
        click.echo(f"  PK-Schluessel: {cfg.zielsystem.upsert_key}")

    click.secho("\ndry-run beendet — nichts wurde geschrieben.", fg="cyan")


if __name__ == "__main__":
    cli()