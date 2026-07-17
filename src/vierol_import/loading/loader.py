"""
Load-Stufe: gemappte Datensaetze in ein Zielsystem schreiben.

Aktueller Ausbaustand: SQLite. Oracle folgt als zweiter Lader; der
Dispatch anhand `zielsystem.typ` ist bereits vorgesehen (siehe unten,
`waehle_lader`).

Design-Entscheidungen fuer SQLite:

  1. Tabellen-Schema wird aus der Config ABGELEITET.
     Zieltabelle + Spalten stehen bereits in der QuellenConfig
     (Mapping-Regeln + abgeleitete Felder + zielsystem.tabelle).
     Kein zweites, redundantes Schema. `CREATE TABLE IF NOT EXISTS`
     legt sie beim ersten Lauf an.

  2. Upsert statt Insert.
     Wenn `zielsystem.upsert_key` gesetzt ist, wird ein INSERT ... ON
     CONFLICT ... DO UPDATE ausgefuehrt: gleiche OE-Nummer + gleicher
     Hersteller ueberschreibt den bestehenden Datensatz. Ohne
     Upsert-Key: plain INSERT.

  3. Transaktion pro Datei.
     Entweder werden ALLE Zeilen einer Datei geladen oder KEINE
     (Rollback bei Fehler). Kein halb geladener Zustand.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.mapping.mapper import MappingErgebnis

logger = logging.getLogger(__name__)

# Python-Typ -> SQLite-Spaltentyp. SQLite ist dynamisch typisiert,
# aber wir setzen die Typ-Affinitaeten trotzdem korrekt, damit
# spaetere Abfragen sich verhalten wie erwartet.
_SQL_TYP = {
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bool: "INTEGER",
    date: "TEXT",       # ISO-Format
    datetime: "TEXT",
    type(None): "TEXT", # Fallback, wenn erste Zeile None hat
}


@dataclass
class LadeErgebnis:
    tabelle: str
    zeilen_geladen: int
    zeilen_uebersprungen: int = 0


def lade_sqlite(
    ergebnis: MappingErgebnis,
    cfg: QuellenConfig,
    db_pfad: Path,
) -> LadeErgebnis:
    """Ein MappingErgebnis in eine SQLite-Datenbank schreiben.

    Legt die Tabelle bei Bedarf an, faehrt alles in einer Transaktion.
    """
    if not ergebnis.saetze:
        logger.warning("Nichts zu laden — 0 Datensaetze.")
        return LadeErgebnis(tabelle=cfg.zielsystem.tabelle, zeilen_geladen=0)

    db_pfad.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_pfad)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        _tabelle_anlegen(con, cfg, ergebnis)
        anzahl = _saetze_schreiben(con, cfg, ergebnis)
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("Fehler beim Laden — Rollback ausgefuehrt.")
        raise
    finally:
        con.close()

    logger.info(
        "Load: %d Datensaetze in Tabelle '%s' (DB %s)",
        anzahl,
        cfg.zielsystem.tabelle,
        db_pfad,
    )
    return LadeErgebnis(tabelle=cfg.zielsystem.tabelle, zeilen_geladen=anzahl)


def _tabelle_anlegen(
    con: sqlite3.Connection, cfg: QuellenConfig, ergebnis: MappingErgebnis
) -> None:
    """CREATE TABLE IF NOT EXISTS aus der Feldliste des ersten Satzes."""
    beispiel = _erste_typisierte_zeile(ergebnis)

    spalten_defs = []
    for feld in ergebnis.zielfelder:
        py_typ = type(beispiel.get(feld))
        sql_typ = _SQL_TYP.get(py_typ, "TEXT")
        spalten_defs.append(f'"{feld}" {sql_typ}')

    # Upsert-Key als UNIQUE-Constraint, damit ON CONFLICT greifen kann
    if cfg.zielsystem.upsert_key:
        cols = ", ".join(f'"{k}"' for k in cfg.zielsystem.upsert_key)
        spalten_defs.append(f"UNIQUE ({cols})")

    ddl = (
        f'CREATE TABLE IF NOT EXISTS "{cfg.zielsystem.tabelle}" (\n  '
        + ",\n  ".join(spalten_defs)
        + "\n)"
    )
    logger.debug("DDL:\n%s", ddl)
    con.execute(ddl)


def _erste_typisierte_zeile(ergebnis: MappingErgebnis) -> dict[str, Any]:
    """Zeile fuer Typinferenz — pro Feld die erste NICHT-None-Auspraegung.

    So verhindern wir, dass optionale Spalten wie `ersatzflag` (in der
    ersten Zeile None) faelschlich als TEXT deklariert werden, wenn sie
    doch mal einen Wert liefern.
    """
    beispiel: dict[str, Any] = dict(ergebnis.saetze[0])
    for feld in ergebnis.zielfelder:
        if beispiel.get(feld) is None:
            for satz in ergebnis.saetze[1:]:
                if satz.get(feld) is not None:
                    beispiel[feld] = satz[feld]
                    break
    return beispiel


def _saetze_schreiben(
    con: sqlite3.Connection, cfg: QuellenConfig, ergebnis: MappingErgebnis
) -> int:
    """Alle Datensaetze per executemany schreiben (Upsert wenn moeglich)."""
    felder = ergebnis.zielfelder
    spalten_liste = ", ".join(f'"{f}"' for f in felder)
    platzhalter = ", ".join("?" for _ in felder)
    tabelle = cfg.zielsystem.tabelle

    sql = f'INSERT INTO "{tabelle}" ({spalten_liste}) VALUES ({platzhalter})'

    if cfg.zielsystem.upsert_key:
        upsert_cols = ", ".join(f'"{k}"' for k in cfg.zielsystem.upsert_key)
        # Nicht-Key-Felder aktualisieren
        update_felder = [f for f in felder if f not in cfg.zielsystem.upsert_key]
        update_clause = ", ".join(
            f'"{f}" = excluded."{f}"' for f in update_felder
        )
        sql += f" ON CONFLICT ({upsert_cols}) DO UPDATE SET {update_clause}"

    # sqlite3 kann datetime.date/datetime nicht direkt — als ISO-String schreiben
    rows = [
        tuple(_zu_sqlite_wert(satz.get(f)) for f in felder)
        for satz in ergebnis.saetze
    ]
    con.executemany(sql, rows)
    return len(rows)


def _zu_sqlite_wert(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v