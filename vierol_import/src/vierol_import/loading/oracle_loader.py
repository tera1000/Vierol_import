"""
Oracle-Loader — produktive Implementierung.

Design-Prinzip: gleiche Semantik wie SQLite-Loader, MIT EINER wichtigen
Ausnahme: der Oracle-Loader legt KEINE Tabellen an. Die Zieltabelle muss
in Oracle bereits existieren (vom DBA oder Fachbereich angelegt).

Grund: automatisches CREATE TABLE birgt Risiken bei produktiven
Oracle-Datenbanken — ein Import-Tool sollte keine impliziten
Schema-Aenderungen an einer Firmen-Datenbank vornehmen koennen.
Tabellenstruktur, Primary Keys, Indizes gehoeren in die bewusste
Kontrolle des Fachbereichs/DBA.

Deshalb: PRUEFT nur ob die Tabelle existiert. Wenn nicht, klare
Fehlermeldung mit Hinweis was zu tun ist — kein automatisches Anlegen.

Gross-/Kleinschreibung von Bezeichnern (WICHTIG):
  Oracle speichert unquoted angelegte Tabellen- UND Spaltennamen immer
  in Grossbuchstaben, unabhaengig davon wie sie im urspruenglichen DDL
  geschrieben wurden. Das ist bei Vierol der Standardfall (Tabellen und
  Spalten werden nicht quoted angelegt). Deshalb werden hier sowohl
  Tabellennamen (_schema_und_tabelle) als auch Spaltennamen (_ora_id)
  konsequent auf Grossbuchstaben normalisiert, BEVOR sie gequoted ins
  SQL eingesetzt werden. Ein quoted Bezeichner ist in Oracle
  case-SENSITIVE — ohne dieses .upper() wuerde z.B. eine Config mit
  `ziel: jahrmonat` (klein, aus der YAML) eine Spalte "jahrmonat"
  suchen, die es nicht gibt (die echte Spalte heisst JAHRMONAT) ->
  ORA-00904: invalid identifier.

Weitere Eigenschaften:
  - pk_konflikt-Modi: skip / update / insert
  - Transaktion pro Datei (alles oder nichts, bei Fehler Rollback)
  - Batch-Insert via executemany fuer Performance

Passwort-Handling: Passwoerter werden vom Aufrufer explizit uebergeben
(GUI-Session-State oder CLI-Prompt). Dieses Modul liest NIE Umgebungs-
variablen oder Dateien mit Passwoertern.

Schema-Unterstuetzung: cfg.zielsystem.tabelle darf entweder ein
einfacher Name sein (z.B. "US_OE_PREISE") oder als "SCHEMA.TABELLE"
(z.B. "DWHRR.US_OE_BRUTTOPREISE_test").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.loading.loader import LadeErgebnis, PKKonfliktFehler
from vierol_import.mapping.mapper import MappingErgebnis

logger = logging.getLogger(__name__)


class OracleNichtVerfuegbar(RuntimeError):
    """Wird geworfen, wenn Credentials fehlen oder oracledb nicht importiert
    werden kann."""


class OracleTabelleFehlt(RuntimeError):
    """Wird geworfen, wenn die Zieltabelle in Oracle nicht existiert.

    Der Loader legt bewusst KEINE Tabellen an — siehe Modul-Docstring.
    """


@dataclass
class OracleBatchFehler:
    """Ein einzelner Zeilen-Fehler aus einem Oracle-Batch-Insert.

    Die Attribut-Namen entsprechen bewusst dem Duck-Typing-Kontrakt in
    monitoring/audit_log.py::_fehler_eintraege_bauen (das dieselben
    Attributnamen wie ValidierungsFehler erwartet: zeile/spalte/wert/
    roh_zeile/grund). Dadurch koennen Oracle-Batch-Fehler ohne Sonderfall
    in die `import_fehler`-Tabelle geschrieben werden — genau wie
    Validierungs-Quarantaenezeilen.

    roh_zeile: die komplette Zeile im Format 'feld1=wert1;feld2=wert2;...'
    fuer die Fehler-Diagnose im Kontext (z.B. Reklamation gegenueber
    Datenlieferant). Wird immer gefuellt.
    """
    zeile: int
    spalte: str | None
    wert: str | None
    grund: str
    roh_zeile: str | None = None


# Modul-globaler Speicher fuer die Batch-Fehler des LETZTEN
# lade_oracle-Aufrufs. Die Engine liest ihn nach dem Load ueber
# hole_letzte_batch_fehler() aus. Dieser Zwischenweg vermeidet, dass
# die generische LadeErgebnis-Struktur (loader.py) um Oracle-spezifische
# Felder erweitert werden muss.
#
# EINSCHRAENKUNG: nicht thread-safe. Fuer den aktuellen sequentiellen
# Batch-Import ok. Wenn spaeter parallele Imports gebaut werden, muss
# das ueber contextvars oder eine Zurueckgabe im LadeErgebnis geloest
# werden.
_LETZTE_BATCH_FEHLER: list[OracleBatchFehler] = []


def hole_letzte_batch_fehler() -> list[OracleBatchFehler]:
    """Gibt die Batch-Fehler des letzten lade_oracle-Aufrufs zurueck.

    Wird von engine.schreibe() aufgerufen, um Oracle-Zeilenfehler in die
    ergebnis.quarantaene_zeilen zu uebertragen und damit ins Audit-Log
    (import_fehler-Tabelle) zu schreiben.

    Rueckgabe: Kopie der Fehlerliste (kann vom Aufrufer beliebig
    veraendert werden, ohne den Modul-Speicher zu beeinflussen).
    """
    return list(_LETZTE_BATCH_FEHLER)


def lade_oracle(
    ergebnis: MappingErgebnis, cfg: QuellenConfig, verbindung,
    passwort: str,
) -> LadeErgebnis:
    """Analog zu lade_sqlite, aber zielt auf Oracle.

    `verbindung` traegt user + dsn, `passwort` wird vom Aufrufer
    explizit uebergeben — dieses Modul liest NIE Umgebungsvariablen
    oder Dateien mit Passwoertern. So bleibt Credential-Management
    vollstaendig in der Verantwortung des Aufrufers (GUI-Session /
    CLI-Prompt).

    WICHTIG: die Zieltabelle muss in Oracle bereits existieren.
    Der Loader legt keine Tabellen an (siehe Modul-Docstring) —
    bei fehlender Tabelle wird OracleTabelleFehlt geworfen.
    """
    # Batch-Fehler-Speicher fuer diesen Lauf frisch initialisieren —
    # die Engine holt sie nach diesem Aufruf via hole_letzte_batch_fehler().
    _LETZTE_BATCH_FEHLER.clear()

    if not ergebnis.saetze:
        logger.info("Oracle: keine Datensaetze zu laden fuer %s",
                    cfg.zielsystem.tabelle)
        return LadeErgebnis(tabelle=cfg.zielsystem.tabelle, zeilen_geladen=0)

    con = _verbindung(verbindung, passwort)
    try:
        cur = con.cursor()
        _tabelle_pruefen(cur, cfg)

        if cfg.zielsystem.pk_konflikt == "insert":
            konflikte = _konflikte_zaehlen(cur, cfg, ergebnis)
            if konflikte > 0:
                raise PKKonfliktFehler(konflikte)

        geladen, uebersprungen = _saetze_schreiben(cur, cfg, ergebnis)
        con.commit()
        logger.info(
            "Oracle: %s -> %d neu, %d uebersprungen",
            cfg.zielsystem.tabelle, geladen, uebersprungen,
        )
        return LadeErgebnis(
            tabelle=cfg.zielsystem.tabelle,
            zeilen_geladen=geladen,
            zeilen_uebersprungen=uebersprungen,
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _verbindung(verbindung, passwort: str):
    """Oracle-Verbindung aufbauen mit expliziten Credentials."""
    if not verbindung.user or not verbindung.dsn:
        raise OracleNichtVerfuegbar(
            f"Verbindung '{verbindung.name}' unvollstaendig — "
            "'user' und 'dsn' muessen gesetzt sein."
        )
    if not passwort:
        raise OracleNichtVerfuegbar(
            f"Kein Passwort fuer Verbindung '{verbindung.name}' uebergeben."
        )

    try:
        import oracledb
    except ImportError as e:
        raise OracleNichtVerfuegbar(
            "Package 'oracledb' nicht installiert. Bitte 'pip install oracledb'."
        ) from e

    try:
        return oracledb.connect(
            user=verbindung.user, password=passwort, dsn=verbindung.dsn,
        )
    except oracledb.Error as e:
        raise OracleNichtVerfuegbar(f"Oracle-Verbindung fehlgeschlagen: {e}") from e


def _schema_und_tabelle(tabelle_ref: str) -> tuple[str | None, str]:
    """Trennt 'SCHEMA.TABELLE' in (schema, tabelle), beide gross normalisiert.

    Siehe Modul-Docstring zur Begruendung des .upper().
    """
    tabelle_ora = tabelle_ref.upper()
    if "." in tabelle_ora:
        schema, tab = tabelle_ora.rsplit(".", 1)
        return schema, tab
    return None, tabelle_ora


def _ora_id(name: str) -> str:
    """Einen Spalten-/Feldnamen zu einem quoted Oracle-Bezeichner machen.

    Immer gross normalisiert, aus demselben Grund wie bei
    _schema_und_tabelle — siehe Modul-Docstring.
    """
    return '"' + name.upper() + '"'


def _tabelle_pruefen(cur, cfg: QuellenConfig) -> None:
    """Prueft ob die Zieltabelle in Oracle existiert.

    Legt NICHTS an. Wenn die Tabelle fehlt, wird OracleTabelleFehlt
    geworfen — mit einer Meldung, die dem Fachbereich sagt was zu tun
    ist.
    """
    schema, tab = _schema_und_tabelle(cfg.zielsystem.tabelle)
    voll_qualifiziert = f'"{schema}"."{tab}"' if schema else f'"{tab}"'

    if schema:
        cur.execute(
            "SELECT COUNT(*) FROM all_tables "
            "WHERE owner = :ow AND table_name = :tn",
            {"ow": schema, "tn": tab},
        )
    else:
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :tn",
            {"tn": tab},
        )
    exists = cur.fetchone()[0] > 0

    if not exists:
        raise OracleTabelleFehlt(
            f"Zieltabelle {voll_qualifiziert} existiert nicht in Oracle "
            f"(Schema {schema or '<Default-Schema des Users>'}).\n"
            f"Dieser Import legt keine Tabellen automatisch an — bitte die "
            f"Tabelle vorher per DDL anlegen (DBA/Fachbereich).\n"
            f"Konfigurierter Wert: zielsystem.tabelle = "
            f"'{cfg.zielsystem.tabelle}'"
        )
    logger.debug("Oracle-Tabelle %s gefunden.", voll_qualifiziert)


def _konflikte_zaehlen(cur, cfg: QuellenConfig, ergebnis: MappingErgebnis) -> int:
    """Vor pk_konflikt=insert: wie viele einzuspielende PKs existieren schon?"""
    if not cfg.zielsystem.upsert_key:
        return 0
    schema, tab = _schema_und_tabelle(cfg.zielsystem.tabelle)
    voll_qualifiziert = f'"{schema}"."{tab}"' if schema else f'"{tab}"'
    keys = cfg.zielsystem.upsert_key

    konflikte = 0
    batchgroesse = 500
    for i in range(0, len(ergebnis.saetze), batchgroesse):
        batch = ergebnis.saetze[i:i + batchgroesse]
        bedingungen = []
        params: dict[str, Any] = {}
        for j, satz in enumerate(batch):
            teil = " AND ".join(
                f'{_ora_id(k)} = :{k}_{j}' for k in keys
            )
            bedingungen.append(f"({teil})")
            for k in keys:
                params[f"{k}_{j}"] = _zu_oracle_wert(satz.get(k))
        sql = (
            f'SELECT COUNT(*) FROM {voll_qualifiziert} '
            f'WHERE {" OR ".join(bedingungen)}'
        )
        cur.execute(sql, params)
        konflikte += cur.fetchone()[0]
    return konflikte


def _hole_existierende_keys(
    cur, cfg: QuellenConfig, ergebnis: MappingErgebnis,
) -> set[tuple]:
    """Ermittelt, welche Business-Key-Kombinationen der zu ladenden Datensaetze
    bereits in der Zieltabelle existieren.

    Rueckgabe: set von tuples; jedes tuple enthaelt die Werte der
    upsert_key-Spalten in der Reihenfolge cfg.zielsystem.upsert_key.

    Der SELECT-basierte Duplikatsschutz ist eine bewusste Loesung fuer
    Zieltabellen ohne UNIQUE-Constraint auf dem Business-Key. Er
    funktioniert unabhaengig von DBA-seitigen Constraints, hat aber
    eine Performance-Grenze: bei sehr grossen Datenmengen ohne
    passenden (nicht-eindeutigen) Index kann die WHERE-Bedingung teuer
    werden. Fuer den produktiven Betrieb wird ein UNIQUE-Constraint
    empfohlen; siehe Ausblick-Kapitel.

    Wenn kein upsert_key gesetzt ist, wird ein leeres Set zurueckgegeben
    — dann findet keine Vorpruefung statt.
    """
    if not cfg.zielsystem.upsert_key:
        return set()

    schema, tab = _schema_und_tabelle(cfg.zielsystem.tabelle)
    voll_qualifiziert = f'"{schema}"."{tab}"' if schema else f'"{tab}"'
    keys = cfg.zielsystem.upsert_key
    key_cols_sql = ", ".join(_ora_id(k) for k in keys)

    existierende: set[tuple] = set()
    # Chunk-Groesse fuer den WHERE-Aufbau: 500 OR-Bedingungen pro Query.
    # Kleiner als _CHUNK_GROESSE (10.000), weil Oracle bei sehr grossen
    # OR-Statements einen Parser-Overhead hat (Statement wird lang).
    batchgroesse = 500
    for i in range(0, len(ergebnis.saetze), batchgroesse):
        batch = ergebnis.saetze[i:i + batchgroesse]
        bedingungen = []
        params: dict[str, Any] = {}
        for j, satz in enumerate(batch):
            teil = " AND ".join(
                f'{_ora_id(k)} = :{k}_{j}' for k in keys
            )
            bedingungen.append(f"({teil})")
            for k in keys:
                params[f"{k}_{j}"] = _zu_oracle_wert(satz.get(k))
        sql = (
            f'SELECT {key_cols_sql} FROM {voll_qualifiziert} '
            f'WHERE {" OR ".join(bedingungen)}'
        )
        cur.execute(sql, params)
        for row in cur.fetchall():
            existierende.add(tuple(row))
    return existierende


def _saetze_schreiben(
    cur, cfg: QuellenConfig, ergebnis: MappingErgebnis
) -> tuple[int, int]:
    """Modus-abhaengiges Schreiben mit SELECT-basiertem Duplikatsschutz.

    Ueberblick der Modi (pk_konflikt):

      - skip:   Zeilen, deren Business-Key bereits in der Zieltabelle
                existiert, werden uebersprungen; die uebrigen werden
                per INSERT eingefuegt. Der Duplikatscheck erfolgt vorher
                per SELECT — funktioniert deshalb auch OHNE
                UNIQUE-Constraint in Oracle.

      - update: Zeilen, deren Business-Key existiert, werden per UPDATE
                aktualisiert; die neuen werden per INSERT eingefuegt.
                Auch hier vorheriger SELECT-Check.

      - insert: Prueft vorab, ob mindestens ein Business-Key bereits
                existiert; falls ja, wird die gesamte Datei
                zurueckgewiesen (PKKonfliktFehler in lade_oracle).
                Kommt der INSERT hier an, sind keine Konflikte da.

    Teilfehler-Toleranz: alle INSERT- und UPDATE-Batches nutzen
    _executemany_tolerant (batcherrors=True + Chunking), damit einzelne
    kaputte Zeilen den Batch nicht killen. Details siehe dortiger
    Docstring.
    """
    schema, tab = _schema_und_tabelle(cfg.zielsystem.tabelle)
    voll_qualifiziert = f'"{schema}"."{tab}"' if schema else f'"{tab}"'
    felder = ergebnis.zielfelder
    modus = cfg.zielsystem.pk_konflikt
    keys = cfg.zielsystem.upsert_key

    # INSERT-Vorlage (fuer skip/insert und den INSERT-Teil von update)
    spalten_sql = ", ".join(_ora_id(f) for f in felder)
    binds_sql = ", ".join(f":{i+1}" for i in range(len(felder)))
    insert_sql = (
        f'INSERT INTO {voll_qualifiziert} '
        f'({spalten_sql}) VALUES ({binds_sql})'
    )

    def _binds(saetze):
        """Aus Mapping-Saetzen die Bind-Werte-Listen fuer executemany bauen."""
        return [
            [_zu_oracle_wert(satz.get(f)) for f in felder]
            for satz in saetze
        ]

    # Original-Zeilen aus dem MappingErgebnis holen (parallel zu
    # ergebnis.saetze). Wenn das Feld leer ist (aeltere Mapping-Version
    # oder ohne Datei-Zeilen), bleibt es leer und der Loader traegt
    # None in roh_zeile ein.
    alle_roh_zeilen: list[str] = getattr(ergebnis, "roh_zeilen", []) or []

    # --- Modus insert ---------------------------------------------------
    # Konfliktpruefung passiert bereits in lade_oracle (_konflikte_zaehlen).
    # Wenn wir hier ankommen, sind keine Konflikte da — reines INSERT.
    if modus == "insert":
        return _executemany_tolerant(
            cur, insert_sql, _binds(ergebnis.saetze), ergebnis, felder,
            roh_zeilen=alle_roh_zeilen, cfg=cfg,
        )

    # --- Modus skip -----------------------------------------------------
    if modus == "skip":
        existierende = _hole_existierende_keys(cur, cfg, ergebnis)

        if not keys:
            # Ohne upsert_key koennen wir keine Duplikate erkennen —
            # Fallback: einfach INSERT und auf DB-Constraints hoffen
            # (die es hier aber gerade nicht gibt). Wir warnen im Log.
            logger.warning(
                "pk_konflikt=skip ohne upsert_key in Config '%s' — "
                "kein Duplikatsschutz moeglich, INSERT ohne Vorpruefung.",
                cfg.name,
            )
            return _executemany_tolerant(
                cur, insert_sql, _binds(ergebnis.saetze), ergebnis, felder,
                roh_zeilen=alle_roh_zeilen, cfg=cfg,
            )

        # Zeilen filtern: nur die, deren Business-Key noch nicht existiert.
        # Parallel dazu die roh_zeilen mitfiltern.
        neue_saetze = []
        neue_roh_zeilen: list[str] = []
        for i, satz in enumerate(ergebnis.saetze):
            key_tuple = tuple(
                _zu_oracle_wert(satz.get(k)) for k in keys
            )
            if key_tuple not in existierende:
                neue_saetze.append(satz)
                # Parallel-Index in alle_roh_zeilen suchen. Fallback ""
                # wenn der Mapper die Zeile nicht mitgeliefert hat.
                if i < len(alle_roh_zeilen):
                    neue_roh_zeilen.append(alle_roh_zeilen[i])
                else:
                    neue_roh_zeilen.append("")

        uebersprungen = len(ergebnis.saetze) - len(neue_saetze)
        if uebersprungen > 0:
            logger.info(
                "Oracle skip: %d Datensaetze existieren bereits "
                "(Business-Key gleich), %d werden neu eingefuegt.",
                uebersprungen, len(neue_saetze),
            )

        if not neue_saetze:
            return 0, uebersprungen

        # Die neuen einfuegen — Toleranz fuer sonstige DB-Fehler bleibt
        geladen_neu, insert_fehler = _executemany_tolerant(
            cur, insert_sql, _binds(neue_saetze), ergebnis, felder,
            roh_zeilen=neue_roh_zeilen, cfg=cfg,
        )
        # insert_fehler sind Zeilen, die ausser wegen "schon existiert"
        # noch andere Probleme hatten (z.B. ORA-12899). Wir addieren sie
        # zu den "uebersprungen" — semantisch dasselbe fuer den Aufrufer.
        return geladen_neu, uebersprungen + insert_fehler

    # --- Modus update ---------------------------------------------------
    if modus == "update":
        if not keys:
            raise ValueError(
                f"pk_konflikt=update braucht upsert_key in Config '{cfg.name}'."
            )

        existierende = _hole_existierende_keys(cur, cfg, ergebnis)

        # Saetze aufteilen: bestehende (UPDATE) vs. neue (INSERT).
        # Parallel dazu die roh_zeilen mitteilen.
        bestehende_saetze = []
        neue_saetze = []
        bestehende_roh: list[str] = []
        neue_roh: list[str] = []
        for i, satz in enumerate(ergebnis.saetze):
            key_tuple = tuple(
                _zu_oracle_wert(satz.get(k)) for k in keys
            )
            roh = alle_roh_zeilen[i] if i < len(alle_roh_zeilen) else ""
            if key_tuple in existierende:
                bestehende_saetze.append(satz)
                bestehende_roh.append(roh)
            else:
                neue_saetze.append(satz)
                neue_roh.append(roh)

        logger.info(
            "Oracle update: %d Datensaetze neu (INSERT), "
            "%d bestehende (UPDATE).",
            len(neue_saetze), len(bestehende_saetze),
        )

        geladen = 0
        uebersprungen = 0

        # Neue Zeilen einfuegen
        if neue_saetze:
            g, u = _executemany_tolerant(
                cur, insert_sql, _binds(neue_saetze), ergebnis, felder,
                roh_zeilen=neue_roh, cfg=cfg,
            )
            geladen += g
            uebersprungen += u

        # Bestehende Zeilen aktualisieren. UPDATE-Statement:
        #   UPDATE tabelle SET col1=:1, col2=:2, ...
        #    WHERE key1=:N AND key2=:N+1 ...
        # Bind-Reihenfolge: erst nicht-Key-Spalten (SET), dann Keys (WHERE).
        if bestehende_saetze:
            nicht_keys = [f for f in felder if f not in keys]
            if nicht_keys:
                set_sql = ", ".join(
                    f'{_ora_id(f)} = :{i+1}'
                    for i, f in enumerate(nicht_keys)
                )
                where_sql = " AND ".join(
                    f'{_ora_id(k)} = :{len(nicht_keys) + i + 1}'
                    for i, k in enumerate(keys)
                )
                update_sql = (
                    f'UPDATE {voll_qualifiziert} '
                    f'SET {set_sql} WHERE {where_sql}'
                )
                update_binds = [
                    [_zu_oracle_wert(satz.get(f)) for f in nicht_keys]
                    + [_zu_oracle_wert(satz.get(k)) for k in keys]
                    for satz in bestehende_saetze
                ]
                g, u = _executemany_tolerant(
                    cur, update_sql, update_binds, ergebnis, felder,
                    roh_zeilen=bestehende_roh, cfg=cfg,
                    bind_felder=nicht_keys + keys,
                )
                geladen += g
                uebersprungen += u
            else:
                # Alle Spalten sind Keys — nichts zu aktualisieren,
                # bestehende bleiben unveraendert.
                logger.debug(
                    "Oracle update: alle Spalten sind Keys — "
                    "bestehende Zeilen bleiben unveraendert.",
                )

        return geladen, uebersprungen

    raise ValueError(f"Unbekannter pk_konflikt: {modus}")


# Maximale Chunk-Groesse fuer executemany-Aufrufe.
#
# Grund: bei batcherrors=True kann der Oracle-Thin-Client (oracledb) pro
# executemany-Aufruf hoechstens 65.535 Fehler tracken — sonst DPY-4029.
# Wenn eine Quelldatei sehr viele Zeilen hat und viele davon an einem
# DB-Constraint scheitern (z.B. VARCHAR2-Ueberlaenge), rennen wir in
# dieses Limit und der komplette Batch schlaegt fehl statt einzelne
# Zeilen zu ueberspringen.
#
# Loesung: die Zeilen in Chunks aufteilen und pro Chunk executemany
# aufrufen. Jeder Chunk kann max. chunk_size Fehler haben, was weit
# unter 65535 bleibt. Der Wert 10.000 ist ein guter Kompromiss:
# gross genug fuer Insert-Performance, klein genug dass eine
# Chunk-lokale Fehlerhaeufung nie das Limit reisst.
_CHUNK_GROESSE = 10_000


def _bind_sizes_gezielt(
    cfg: QuellenConfig, felder: list[str],
) -> list | None:
    """Bind-Groessen NUR fuer Felder mit explizit gesetztem max_laenge.

    Hintergrund — empirisch validierter Fund aus der Prototyp-
    Erprobung: oracledb bestimmt die Bind-Breite einer Spalte anhand
    des laengsten Werts im aktuellen Batch/Chunk. Enthaelt ein Chunk
    von 10.000 Zeilen auch nur EINE Zeile mit einem laengeren Wert in
    einer eng dimensionierten Spalte (z.B. CHAR(1) fuer ersatzflag),
    wird die Bind-Breite fuer den GESAMTEN Chunk hochgezogen. Oracle
    lehnt dann JEDE Zeile dieses Chunks fuer diese Spalte ab — auch
    Zeilen mit einem tatsaechlich passenden 1-Zeichen-Wert — mit
    "ORA-12899: actual X, maximum 1". Betroffene Zeilen wechseln
    zufaellig je nachdem, welchem Chunk sie zugeordnet sind.

    Fix: vor jedem executemany die Bind-Breite explizit vorgeben
    (cursor.setinputsizes()), damit oracledb den Chunk-internen
    Maximalwert ignoriert.

    Bewusst EINGESCHRAENKTER Scope (im Gegensatz zu einem frueheren,
    zurueckgerollten Versuch mit Default-Groesse fuer ALLE Felder):
    nur Felder, deren Quell-Spalte in der Config ein explizites
    max_laenge traegt, bekommen eine erzwungene Bind-Groesse. Alle
    anderen Felder (kein max_laenge gesetzt, oder system/berechnet)
    bleiben unangetastet — oracledb entscheidet dort wie bisher
    automatisch. Das haelt den Eingriff minimal und vermeidet, fuer
    Felder ohne Laengenbegrenzung einen kuenstlichen Default (z.B.
    4000) zu erzwingen.

    Rueckgabe: Liste in Reihenfolge von `felder`, oder None wenn KEIN
    Feld ein max_laenge hat (dann wird setinputsizes gar nicht erst
    aufgerufen).
    """
    spalten_nach_name = {s.name: s for s in cfg.spalten}

    ziel_zu_quelle: dict[str, str] = {}
    if cfg.mapping.felder:
        for feld in cfg.mapping.felder:
            if feld.kategorie == "quelle" and feld.von:
                ziel_zu_quelle[feld.ziel] = feld.von
    else:
        for regel in cfg.mapping.regeln:
            ziel_zu_quelle[regel.ziel] = regel.quelle

    sizes: list = []
    irgendeine_groesse_gesetzt = False
    for ziel in felder:
        quelle_name = ziel_zu_quelle.get(ziel)
        sp = spalten_nach_name.get(quelle_name) if quelle_name else None
        if sp is not None and sp.typ == "string" and sp.max_laenge is not None:
            sizes.append(sp.max_laenge)
            irgendeine_groesse_gesetzt = True
        else:
            sizes.append(None)

    if not irgendeine_groesse_gesetzt:
        return None
    return sizes


def _executemany_tolerant(
    cur, sql: str, zeilen: list, ergebnis: MappingErgebnis, felder: list[str],
    roh_zeilen: list[str] | None = None,
    cfg: QuellenConfig | None = None,
    bind_felder: list[str] | None = None,
) -> tuple[int, int]:
    """executemany mit batcherrors=True in Chunks: einzelne kaputte Zeilen
    werden uebersprungen und geloggt statt den ganzen Batch zu killen.

    Rueckgabe: (geladen, uebersprungen). 'uebersprungen' zaehlt sowohl
    PK-Konflikte (ORA-00001 bei skip) als auch alle anderen DB-Fehler
    (z.B. ORA-12899 bei zu langen Werten, ORA-01861 bei Datumsformat-
    Problemen etc.) — jede Art von Zeilen-Fehler blockiert nur die
    betroffene Zeile, nicht den Rest.

    Die Aufteilung in Chunks von _CHUNK_GROESSE Zeilen umgeht das
    oracledb-Limit von 65.535 batcherrors pro executemany-Aufruf
    (DPY-4029).

    `roh_zeilen`: optional. Parallele Liste zu `zeilen` — enthaelt pro
    Bind-Wert-Zeile die zugehoerige ORIGINAL-DATEI-ZEILE (durch
    Validator + Mapper durchgereicht). Bei einem Fehler wird die
    Original-Zeile direkt in das OracleBatchFehler-Objekt geschrieben.

    `cfg` + `bind_felder`: wenn cfg uebergeben wird, ruft die Funktion
    vor jedem executemany `cur.setinputsizes(*sizes)` auf — aber NUR
    fuer Felder mit explizit gesetztem max_laenge in der Config (siehe
    _bind_sizes_gezielt-Docstring fuer den Hintergrund). `bind_felder`
    ist die Reihenfolge der Bind-Positionen im SQL; fuer INSERT ist das
    identisch mit `felder`, fuer UPDATE ist es `nicht_keys + keys`
    (SET-Klausel zuerst, dann WHERE).
    """
    # Bind-Groessen einmal berechnen (nur wenn cfg uebergeben wurde).
    # _bind_sizes_gezielt gibt None zurueck, wenn kein Feld ein
    # max_laenge hat — dann bleibt bind_sizes None und setinputsizes
    # wird gar nicht erst aufgerufen (kein Verhaltens-Unterschied zu
    # vorher fuer Configs ohne max_laenge-Angaben).
    bind_sizes = None
    if cfg is not None:
        reihenfolge = bind_felder if bind_felder is not None else felder
        bind_sizes = _bind_sizes_gezielt(cfg, reihenfolge)

    # Fehler werden ueber alle Chunks hinweg gesammelt. Zwei Ziele:
    # (1) strukturierte Ablage in _LETZTE_BATCH_FEHLER fuer die
    #     spaetere Auswertung durch die Engine (-> import_fehler-Tabelle)
    # (2) kompakte Konsolen-Ausgabe (erste _MAX_DETAIL_FEHLER Detail-
    #     Meldungen + Rest aggregiert nach Fehler-Muster).
    # alle_fehler haelt neben Zeilennummer/Nachricht auch die extrahierte
    # Spalte und den konkreten Wert — so kann die Aggregation je Muster
    # ein Beispiel-Wert zeigen ('Wert: ROTIERE' statt nur 'ORA-12899').
    alle_fehler: list[tuple[int, str, str | None, str | None]] = []
    # (zeilennummer, message, spalte, wert)

    for chunk_start in range(0, len(zeilen), _CHUNK_GROESSE):
        chunk = zeilen[chunk_start:chunk_start + _CHUNK_GROESSE]
        # Vor JEDEM executemany die Bind-Groessen setzen (falls welche
        # ermittelt wurden) — sonst kann sich oracledb zwischen Chunks
        # eine hoehere Breite merken und nachfolgende Chunks kippen.
        if bind_sizes is not None:
            cur.setinputsizes(*bind_sizes)
        cur.executemany(sql, chunk, batcherrors=True)
        fehler = cur.getbatcherrors()

        for e in fehler:
            # offset ist chunk-lokal 0-basiert; addieren des chunk_start
            # macht die Zeilennummer wieder zur Position im gesamten
            # Batch. Achtung: das ist NICHT die Zeilennummer in der
            # Original-Datei — die Validierung hat vorher Zeilen
            # ausgesondert. Fuer eine grobe Verortung reicht es aber.
            zeile_nr = chunk_start + e.offset + 1
            msg = e.message.strip()

            # Strukturierten Eintrag fuer import_fehler bauen: die
            # betroffene Spalte extrahieren wir aus der ORA-Meldung,
            # den konkreten Wert holen wir per felder-Index aus den
            # Bind-Werten der betroffenen Zeile.
            spalte, wert = _spalte_und_wert_aus_fehler(
                msg, chunk[e.offset], felder,
            )
            # Original-Zeile aus der Quelldatei (durch Validator+Mapper
            # durchgereicht). Bei Nicht-Verfuegbarkeit bleibt sie leer.
            roh_zeile_str: str | None = None
            if roh_zeilen is not None:
                idx_in_zeilen = chunk_start + e.offset
                if 0 <= idx_in_zeilen < len(roh_zeilen):
                    roh_zeile_str = roh_zeilen[idx_in_zeilen]

            alle_fehler.append((zeile_nr, msg, spalte, wert))
            _LETZTE_BATCH_FEHLER.append(
                OracleBatchFehler(
                    zeile=zeile_nr,
                    spalte=spalte,
                    wert=wert,
                    grund=msg,
                    roh_zeile=roh_zeile_str,
                )
            )

    uebersprungen_gesamt = len(alle_fehler)
    geladen = len(zeilen) - uebersprungen_gesamt

    if alle_fehler:
        _logge_batch_fehler_zusammengefasst(alle_fehler)

    return geladen, uebersprungen_gesamt


def _spalte_und_wert_aus_fehler(
    ora_msg: str, zeile_werte: list, felder: list[str],
) -> tuple[str | None, str | None]:
    """Extrahiert (spaltenname, wert) aus einer Oracle-Fehlermeldung.

    Oracle-Meldungen wie ORA-12899 nennen die Spalte im Format
      "SCHEMA"."TABELLE"."SPALTE"
    Wir extrahieren den Spaltennamen per Regex und schauen dann in der
    felder-Liste (Reihenfolge = Bind-Position) an welcher Index-Position
    die Spalte steht, um den konkreten Wert aus der Zeile zu holen.

    Wenn die Meldung keinen Spaltennamen enthaelt (viele ORA-Codes
    nennen nur eine allgemeine Ursache), gibt es einfach (None, None)
    zurueck — der grund im OracleBatchFehler enthaelt dann immerhin
    die volle ORA-Meldung.
    """
    import re as _re

    m = _re.search(r'"[^"]+"\."[^"]+"\."([^"]+)"', ora_msg)
    if not m:
        return None, None

    spalte_ora = m.group(1)  # z.B. "ERSATZFLAG" (immer gross)
    # Config-seitige Feldnamen sind meist klein (z.B. "ersatzflag");
    # case-insensitiver Vergleich.
    spalte_lower = spalte_ora.lower()
    for idx, f in enumerate(felder):
        if f.lower() == spalte_lower:
            if idx < len(zeile_werte):
                wert = zeile_werte[idx]
                wert_str = None if wert is None else str(wert)
                # Sehr lange Werte kuerzen — sonst verstopfen sie die
                # Fehleranzeige (z.B. 18.000-Zeichen-BEZEICHNUNG).
                if wert_str and len(wert_str) > 100:
                    wert_str = wert_str[:100] + "..."
                return f, wert_str
            return f, None
    return spalte_ora, None


# Wie viele Detail-Fehler wir maximal einzeln aufs Log schreiben,
# bevor wir aggregieren. Bei 4000+ identischen Fehlern will niemand
# 4000 Zeilen Konsolen-Output.
_MAX_DETAIL_FEHLER = 5


def _logge_batch_fehler_zusammengefasst(
    alle_fehler: list[tuple[int, str, str | None, str | None]],
) -> None:
    """Batch-Fehler kompakt loggen statt jeden einzeln.

    Format je Datensatz: (zeilennummer, ora_message, spalte, wert)
      * spalte/wert wurden aus der Meldung extrahiert (kann None sein
        wenn Oracle keinen Spaltennamen liefert).

    Strategie:
      1. Die ersten _MAX_DETAIL_FEHLER Fehler mit vollem Detail
         (Zeilennummer + Message + Wert falls bekannt).
      2. Danach eine Aggregation: pro eindeutigem Fehlermuster
         (ORA-Code + Spalte) einen Zaehler + ein Beispiel-Wert.
      3. Gesamt-Summe am Ende.

    Damit sind bei vielen gleichartigen Fehlern (typisch bei
    Datenqualitaets-Problemen wie "Spalte X immer zu lang") die
    Konsolen-Zeilen konstant kurz, nicht linear in der Fehlerzahl —
    und der Anwender sieht auf einen Blick welche Werte konkret
    das Problem sind.
    """
    from collections import Counter
    import re as _re

    # Erste N Detail-Meldungen — Wert an die Message anhaengen wenn vorhanden
    fuer_detail = alle_fehler[:_MAX_DETAIL_FEHLER]
    for zeile_nr, msg, _spalte, wert in fuer_detail:
        if wert is not None:
            logger.warning(
                "Oracle: Zeile %d uebersprungen — %s [Wert: %r]",
                zeile_nr, msg, wert,
            )
        else:
            logger.warning(
                "Oracle: Zeile %d uebersprungen — %s", zeile_nr, msg,
            )

    if len(alle_fehler) > _MAX_DETAIL_FEHLER:
        # Muster extrahieren: ORA-Code + Spaltenname (falls in Meldung)
        # Beispiel-Wert pro Muster mitspeichern — der erste gefundene
        # Wert je Muster reicht als Diagnose-Anker
        muster_pattern = _re.compile(
            r'(ORA-\d+).*?"[^"]+"\."[^"]+"\."([^"]+)"'
        )
        muster_pattern_kurz = _re.compile(r'(ORA-\d+)')

        muster_zaehler: Counter[str] = Counter()
        muster_beispiel: dict[str, str] = {}  # muster -> beispiel_wert
        for _, msg, _spalte, wert in alle_fehler[_MAX_DETAIL_FEHLER:]:
            m = muster_pattern.search(msg)
            if m:
                muster = f"{m.group(1)} (Spalte {m.group(2)})"
            else:
                m = muster_pattern_kurz.search(msg)
                muster = m.group(1) if m else "unbekannt"
            muster_zaehler[muster] += 1
            # Ersten bekannten Wert je Muster merken
            if wert is not None and muster not in muster_beispiel:
                muster_beispiel[muster] = wert

        rest = len(alle_fehler) - _MAX_DETAIL_FEHLER
        logger.warning(
            "Oracle: ... und %d weitere Zeilen uebersprungen. Verteilung:",
            rest,
        )
        for muster, anzahl in muster_zaehler.most_common():
            beispiel = muster_beispiel.get(muster)
            if beispiel is not None:
                logger.warning(
                    "  %s: %dx  [Beispiel-Wert: %r]",
                    muster, anzahl, beispiel,
                )
            else:
                logger.warning("  %s: %dx", muster, anzahl)

    logger.warning(
        "Oracle: Insgesamt %d von %d+ Zeilen wegen DB-Fehlern uebersprungen.",
        len(alle_fehler), len(alle_fehler),
    )


def _zu_oracle_wert(v: Any) -> Any:
    """Python-Werte in Oracle-taugliche Form bringen.

    Oracle-Treiber akzeptiert datetime/date direkt. Bool wird zu 0/1.
    None passt als NULL durch.
    """
    if isinstance(v, bool):
        return 1 if v else 0
    return v