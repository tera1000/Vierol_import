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

# Zweite Tabelle: detaillierte Fehler pro Lauf. Bei einem Import mit
# z. B. 18 fehlerhaften Zeilen entstehen 18 Eintraege hier — jeder mit
# Zeilennummer, Spaltenname, dem konkreten fehlerhaften Zellwert und
# dem praezisen Grund. Damit ist per SQL abfragbar:
#   SELECT * FROM import_fehler WHERE lauf_id = 47
# oder aggregiert:
#   SELECT grund, COUNT(*) FROM import_fehler GROUP BY grund
_FEHLER_TABELLE_DDL = """
CREATE TABLE IF NOT EXISTS import_fehler (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lauf_id      INTEGER NOT NULL,
    zeilennummer INTEGER,
    spalte       TEXT,
    wert         TEXT,
    roh_zeile    TEXT,
    grund        TEXT NOT NULL,
    FOREIGN KEY (lauf_id) REFERENCES import_lauf(id)
);
"""

_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_lauf_zeit ON import_lauf(zeitstempel DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_hash ON import_lauf(dateihash)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_quelle ON import_lauf(quelle)",
    "CREATE INDEX IF NOT EXISTS idx_lauf_status ON import_lauf(status)",
    "CREATE INDEX IF NOT EXISTS idx_fehler_lauf ON import_fehler(lauf_id)",
    "CREATE INDEX IF NOT EXISTS idx_fehler_grund ON import_fehler(grund)",
)


def stelle_tabelle_sicher(db_pfad: Path) -> None:
    """Tabelle + Indizes anlegen (idempotent).

    Wird beim ersten Aufruf von `logge_lauf` automatisch getriggert;
    kann aber auch explizit beim Programmstart aufgerufen werden.

    Enthaelt eine kleine Schema-Migration fuer bestehende DBs:
      - fehlende Spalten in import_fehler nachziehen (spalte, wert)
      - alte 'roh_werte'-Spalte entfernen (SQLite >= 3.35;
        sonst bleibt sie stehen und wird ignoriert)
    """
    db_pfad.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_pfad) as con:
        con.execute(_TABELLE_DDL)
        con.execute(_FEHLER_TABELLE_DDL)

        # Migration import_fehler: 'spalte' + 'wert' + 'roh_zeile' ergaenzen,
        # 'roh_werte' entfernen (falls aus aeltester Version noch da)
        vorhanden = {
            row[1] for row in
            con.execute("PRAGMA table_info(import_fehler)").fetchall()
        }
        if "spalte" not in vorhanden:
            con.execute("ALTER TABLE import_fehler ADD COLUMN spalte TEXT")
        if "wert" not in vorhanden:
            con.execute("ALTER TABLE import_fehler ADD COLUMN wert TEXT")
        if "roh_zeile" not in vorhanden:
            con.execute("ALTER TABLE import_fehler ADD COLUMN roh_zeile TEXT")
        if "roh_werte" in vorhanden:
            try:
                con.execute("ALTER TABLE import_fehler DROP COLUMN roh_werte")
            except sqlite3.OperationalError as e:
                logger.info("Kann alte Spalte 'roh_werte' nicht droppen: %s", e)

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
    fehler_details: list[str] | None = None,
    quarantaene_zeilen: list | None = None,
    dauer_ms: int | None = None,
    benutzer_modus: str = "unbekannt",
) -> int | None:
    """Einen Eintrag in `import_lauf` schreiben — plus die einzelnen
    Fehler-Details in `import_fehler` (wenn welche uebergeben werden).

    Rueckgabe: die frisch generierte lauf_id, oder None wenn das
    Schreiben fehlgeschlagen ist.

    Fehler beim Schreiben werden geloggt, aber NICHT weitergereicht —
    ein Audit-Fehler darf nie den fachlichen Import kaputt machen.
    """
    stelle_tabelle_sicher(db_pfad)
    lauf_id: int | None = None
    try:
        with sqlite3.connect(db_pfad) as con:
            cur = con.cursor()
            cur.execute(
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
            lauf_id = cur.lastrowid

            # Fehler-Details schreiben. Zwei Quellen:
            #   1. quarantaene_zeilen: strukturierte
            #      ValidierungsFehler-Objekte (bevorzugt — bringen
            #      spalte + wert + grund sauber getrennt mit)
            #   2. fehler_details: Text-Strings, werden per Regex
            #      wieder zurueckgeparst (Fallback)
            eintraege = _fehler_eintraege_bauen(
                lauf_id, fehler_details, quarantaene_zeilen,
            )
            if eintraege:
                cur.executemany(
                    """
                    INSERT INTO import_fehler
                        (lauf_id, zeilennummer, spalte, wert, roh_zeile, grund)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    eintraege,
                )
            con.commit()
    except sqlite3.Error as e:
        logger.error("Audit-Log konnte nicht geschrieben werden: %s", e)
    return lauf_id


def _fehler_eintraege_bauen(
    lauf_id: int,
    fehler_details: list[str] | None,
    quarantaene_zeilen: list | None,
) -> list[tuple]:
    """Detaillierte Fehler in DB-Zeilen bauen.

    Rueckgabe pro Zeile: (lauf_id, zeilennummer, spalte, wert, roh_zeile, grund)

    Bevorzugt `quarantaene_zeilen` — die enthalten strukturierte
    Fehler-Objekte (ValidierungsFehler oder OracleBatchFehler) mit
    sauber getrennten Feldern spalte, wert, roh_zeile und grund. Fallback
    ist `fehler_details` mit Text-Parsing (bekommt keine roh_zeile).
    """
    import re
    eintraege: list[tuple] = []

    if quarantaene_zeilen:
        for eintrag in quarantaene_zeilen:
            # Struktur 1: strukturiertes Fehler-Objekt (bevorzugt).
            # Duck-Typing statt Import — vermeidet Zirkular-Abhaengigkeit
            # zwischen monitoring und validation/loading.
            if hasattr(eintrag, "zeile") and hasattr(eintrag, "grund"):
                nr = eintrag.zeile
                spalte = getattr(eintrag, "spalte", None)
                wert = getattr(eintrag, "wert", None)
                roh_zeile = getattr(eintrag, "roh_zeile", None)
                # wert kann None oder ein beliebiger Wert sein — als
                # String speichern, um die SQLite-Spalte zu fuellen.
                wert_str = None if wert is None else str(wert)
                roh_str = None if roh_zeile is None else str(roh_zeile)
                grund = eintrag.grund
            else:
                # Struktur 2: alte Tuple-Variante (nr, spalten, grund).
                try:
                    nr, _, grund = eintrag
                except (ValueError, TypeError):
                    nr, grund = None, str(eintrag)
                spalte = None
                wert_str = None
                roh_str = None
            eintraege.append(
                (lauf_id, nr, spalte, wert_str, roh_str, grund or "unbekannt")
            )
        return eintraege

    if fehler_details:
        # Text-Fallback: hier gibt es keine roh_zeile.
        voll_pattern = re.compile(
            r"^Zeile\s+(\d+),\s*Spalte\s+'([^']*)':\s*'([^']*)'\s+[\u2014-]+\s*(.*)$"
        )
        kurz_pattern = re.compile(r"^Zeile\s+(\d+):\s*(.*)$")
        for text in fehler_details:
            m = voll_pattern.match(text)
            if m:
                nr = int(m.group(1))
                spalte = m.group(2)
                wert = m.group(3)
                grund = m.group(4)
            else:
                m = kurz_pattern.match(text)
                if m:
                    nr, spalte, wert, grund = int(m.group(1)), None, None, m.group(2)
                else:
                    nr, spalte, wert, grund = None, None, None, text
            eintraege.append((lauf_id, nr, spalte, wert, None, grund))

    return eintraege


def hole_letzte(db_pfad: Path, anzahl: int = 20) -> list[dict]:
    """Die letzten N Eintraege abrufen (fuer CLI-Anzeige)."""
    stelle_tabelle_sicher(db_pfad)
    with sqlite3.connect(db_pfad) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, zeitstempel, dateiname, quelle, status,
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