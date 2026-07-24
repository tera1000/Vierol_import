"""
Audit-Log als SQLite-Tabelle im selben Datenbank-File wie die Nutzdaten.

Zweck: dauerhaftes, abfragbares Protokoll aller Import-Vorgaenge. Statt
im Log-File nach Textzeilen zu suchen, kann der Fachbereich mit einer
SQL-Abfrage direkt Fragen beantworten wie:
  - "Wie viele Dateien wurden diesen Monat geladen?"
  - "Welche Quelle hat die meisten Rejects?"
  - "Wurde die Datei X (per Hash) schon einmal geladen?"

Die Tabelle heisst `import_lauf`. Fuer jeden Datei-Durchlauf wird
GENAU EIN Eintrag geschrieben — egal ob Erfolg oder Ablehnung. So
bleibt das Log vollstaendig auditierbar.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TABELLE_DDL = """
CREATE TABLE IF NOT EXISTS import_lauf (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    zeitstempel          TEXT NOT NULL,
    dateiname            TEXT NOT NULL,
    dateihash            TEXT,
    quelle               TEXT,
    score                REAL,
    status               TEXT NOT NULL,
    zeilen_gesamt        INTEGER,
    zeilen_geladen       INTEGER,
    zeilen_uebersprungen INTEGER,
    zeilen_quarantaene   INTEGER,
    fehler_grund         TEXT,
    dauer_ms             INTEGER,
    benutzer_modus       TEXT
);
"""

_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_lauf_zeit ON import_lauf(zeitstempel DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_hash ON import_lauf(dateihash)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_quelle ON import_lauf(quelle)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_status ON import_lauf(status)",
)


def stelle_tabelle_sicher(db_pfad: Path) -> None:
    """Tabelle + Indizes anlegen (idempotent).

    Wird beim ersten Aufruf von `logge_lauf` automatisch getriggert;
    kann aber auch explizit beim Programmstart aufgerufen werden.
    """
    db_pfad.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_pfad) as con:
        con.execute(_TABELLE_DDL)
        for ddl in _INDEX_DDLS:
            con.execute(ddl)
        con.commit()


def logge_lauf(
    db_pfad: Path,
    *,
    dateiname: str,
    status: str,
    quelle: str | None = None,
    score: float | None = None,
    dateihash: str | None = None,
    zeilen_gesamt: int = 0,
    zeilen_geladen: int = 0,
    zeilen_uebersprungen: int = 0,
    zeilen_quarantaene: int = 0,
    fehler_grund: str = "",
    dauer_ms: int | None = None,
    benutzer_modus: str = "unbekannt",
) -> None:
    """Einen Eintrag in `import_lauf` schreiben.

    Fehler beim Schreiben werden geloggt, aber NICHT weitergereicht — 
    ein Audit-Fehler darf nie den fachlichen Import kaputt machen.
    """
    stelle_tabelle_sicher(db_pfad)
    try:
        with sqlite3.connect(db_pfad) as con:
            con.execute(
                """
                INSERT INTO import_lauf (
                    zeitstempel, dateiname, dateihash, quelle, score,
                    status, zeilen_gesamt, zeilen_geladen,
                    zeilen_uebersprungen, zeilen_quarantaene,
                    fehler_grund, dauer_ms, benutzer_modus
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    dateiname, dateihash, quelle, score,
                    status, zeilen_gesamt, zeilen_geladen,
                    zeilen_uebersprungen, zeilen_quarantaene,
                    fehler_grund, dauer_ms, benutzer_modus,
                ),
            )
            con.commit()
    except sqlite3.Error as e:
        logger.error("Audit-Log konnte nicht geschrieben werden: %s", e)


def hole_letzte(db_pfad: Path, anzahl: int = 20) -> list[dict]:
    """Die letzten N Eintraege abrufen (fuer CLI-Anzeige)."""
    stelle_tabelle_sicher(db_pfad)
    with sqlite3.connect(db_pfad) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT zeitstempel, dateiname, quelle, status,
                   zeilen_geladen, zeilen_quarantaene, fehler_grund
              FROM import_lauf
             ORDER BY id DESC
             LIMIT ?
            """,
            (anzahl,),
        ).fetchall()
    return [dict(r) for r in rows]


def dateihash_wurde_geladen(db_pfad: Path, dateihash: str) -> bool:
    """Wurde diese Datei (per Inhalts-Hash) schon einmal erfolgreich geladen?

    Praktisch fuer die GUI/CLI, um vor doppeltem Import zu warnen.
    """
    stelle_tabelle_sicher(db_pfad)
    with sqlite3.connect(db_pfad) as con:
        row = con.execute(
            "SELECT 1 FROM import_lauf WHERE dateihash = ? AND status = 'geladen' LIMIT 1",
            (dateihash,),
        ).fetchone()
    return row is not None