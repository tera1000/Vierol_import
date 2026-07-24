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


class PKKonfliktFehler(Exception):
    """Wird geworfen, wenn `pk_konflikt=reject` und mind. ein Konflikt
    auftrat. Loest den Transaktions-Rollback aus."""

    def __init__(self, anzahl: int) -> None:
        super().__init__(
            f"{anzahl} Datensatz/-saetze existieren bereits (pk_konflikt=reject)"
        )
        self.anzahl = anzahl

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
    zeilen_uebersprungen: int = 0     # bei pk_konflikt=skip
    konflikte_erkannt: int = 0        # bei pk_konflikt=reject: Zaehler VOR Rollback


def lade_sqlite(
    ergebnis: MappingErgebnis,
    cfg: QuellenConfig,
    db_pfad: Path,
) -> LadeErgebnis:
    """Ein MappingErgebnis in eine SQLite-Datenbank schreiben.

    Legt die Tabelle bei Bedarf an, faehrt alles in einer Transaktion.
    Verhalten bei PK-Konflikt richtet sich nach `cfg.zielsystem.pk_konflikt`:

      - skip:   bestehende Datensaetze werden nicht ueberschrieben
      - update: bestehende Datensaetze werden ueberschrieben
      - reject: erster Konflikt loest Rollback der ganzen Datei aus
                (PKKonfliktFehler)
    """
    if not ergebnis.saetze:
        logger.warning("Nichts zu laden — 0 Datensaetze.")
        return LadeErgebnis(tabelle=cfg.zielsystem.tabelle, zeilen_geladen=0)

    db_pfad.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_pfad)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        _tabelle_anlegen(con, cfg, ergebnis)
        geladen, uebersprungen, konflikte = _saetze_schreiben(con, cfg, ergebnis)
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("Fehler beim Laden — Rollback ausgefuehrt.")
        raise
    finally:
        con.close()

    logger.info(
        "Load: %d geladen, %d uebersprungen (Modus %s) -> Tabelle '%s' in %s",
        geladen,
        uebersprungen,
        cfg.zielsystem.pk_konflikt,
        cfg.zielsystem.tabelle,
        db_pfad,
    )
    return LadeErgebnis(
        tabelle=cfg.zielsystem.tabelle,
        zeilen_geladen=geladen,
        zeilen_uebersprungen=uebersprungen,
        konflikte_erkannt=konflikte,
    )


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
) -> tuple[int, int, int]:
    """Alle Datensaetze schreiben, PK-Konflikte gemaess Config behandeln.

    Rueckgabe: (geladen, uebersprungen, konflikte_gesamt).
    """
    felder = ergebnis.zielfelder
    spalten_liste = ", ".join(f'"{f}"' for f in felder)
    platzhalter = ", ".join("?" for _ in felder)
    tabelle = cfg.zielsystem.tabelle
    modus = cfg.zielsystem.pk_konflikt
    has_pk = bool(cfg.zielsystem.upsert_key)

    base_sql = f'INSERT INTO "{tabelle}" ({spalten_liste}) VALUES ({platzhalter})'

    if not has_pk:
        # Ohne PK gibt es keine Konflikte -> reiner Insert
        rows = [tuple(_zu_sqlite_wert(s.get(f)) for f in felder) for s in ergebnis.saetze]
        con.executemany(base_sql, rows)
        return len(rows), 0, 0

    upsert_cols = ", ".join(f'"{k}"' for k in cfg.zielsystem.upsert_key)

    if modus == "skip":
        sql = base_sql + f" ON CONFLICT ({upsert_cols}) DO NOTHING"
    elif modus == "update":
        update_felder = [f for f in felder if f not in cfg.zielsystem.upsert_key]
        set_clause = ", ".join(f'"{f}" = excluded."{f}"' for f in update_felder)
        sql = base_sql + f" ON CONFLICT ({upsert_cols}) DO UPDATE SET {set_clause}"
    else:  # reject
        # Fuer Konflikt-Zaehlung: Vorher pruefen, welche PKs schon existieren
        konflikte = _konflikte_zaehlen(con, cfg, ergebnis)
        if konflikte > 0:
            raise PKKonfliktFehler(konflikte)
        sql = base_sql

    rows = [tuple(_zu_sqlite_wert(s.get(f)) for f in felder) for s in ergebnis.saetze]

    # Bei skip: Anzahl der uebersprungenen Zeilen ist Gesamt - geaenderte Zeilen
    # SQLite's `changes()` liefert die zuletzt geaenderte Anzahl. Wir zaehlen
    # daher Zeile fuer Zeile (executemany waere effizienter, verliert aber
    # den Skip-Zaehler).
    geladen = 0
    for row in rows:
        cur = con.execute(sql, row)
        if cur.rowcount > 0:
            geladen += 1
    uebersprungen = len(rows) - geladen
    return geladen, uebersprungen, 0


def _konflikte_zaehlen(
    con: sqlite3.Connection, cfg: QuellenConfig, ergebnis: MappingErgebnis
) -> int:
    """Zaehlt, wie viele Datensaetze bereits per PK in der Tabelle existieren.

    Nur genutzt bei `pk_konflikt=reject`, um vor dem Insert zu entscheiden."""
    tabelle = cfg.zielsystem.tabelle
    keys = cfg.zielsystem.upsert_key
    where = " AND ".join(f'"{k}" = ?' for k in keys)
    sql = f'SELECT COUNT(*) FROM "{tabelle}" WHERE {where}'

    konflikte = 0
    for satz in ergebnis.saetze:
        row = tuple(_zu_sqlite_wert(satz.get(k)) for k in keys)
        if con.execute(sql, row).fetchone()[0] > 0:
            konflikte += 1
    return konflikte


def _zu_sqlite_wert(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


# --- Dispatcher: Lader nach Zielsystem-Typ auswaehlen -----------------------


def lade(
    ergebnis: MappingErgebnis, cfg: QuellenConfig, db_pfad: Path
) -> LadeErgebnis:
    """Zentraler Einstiegspunkt: waehlt anhand `cfg.zielsystem.typ`
    den passenden Lader.

    Aktuell unterstuetzte Typen:
      - sqlite (implementiert)
      - oracle (Stub — im Buero zu vervollstaendigen)

    Neue Zielsysteme (z. B. Postgres) sind hier durch einfaches
    Ergaenzen einer Zeile plus eigenem Loader-Modul einbindbar.
    """
    typ = cfg.zielsystem.typ
    if typ == "sqlite":
        return lade_sqlite(ergebnis, cfg, db_pfad)
    if typ == "oracle":
        # Import verzoegert, damit oracledb nur bei Bedarf importiert
        # wird (verhindert Warnungen, wenn das Package nicht installiert
        # ist und man nur SQLite nutzt).
        from vierol_import.loading.oracle_loader import lade_oracle
        return lade_oracle(ergebnis, cfg, db_pfad)
    raise ValueError(
        f"Unbekanntes Zielsystem '{typ}' in Config '{cfg.name}'. "
        f"Unterstuetzt: sqlite, oracle."
    )