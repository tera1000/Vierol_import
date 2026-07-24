"""
Oracle-Loader — Stub-Implementierung fuer den Bueroeinsatz.

Aktueller Ausbaustand: die Klassen und Aufruf-Signaturen stehen und
werden vom Dispatcher (`loader.lade`) korrekt angesprungen. Die
eigentliche Verbindung + INSERT/UPSERT-Logik wird im Buero
implementiert, sobald Test-Credentials fuer eine Oracle-Instanz
vorliegen.

Wichtig fuer die Architektur (BA-Kolloquium): dieser Stub demonstriert,
dass die Engine, das Mapping und die Vorschau/Two-Phase-Logik DB-
agnostisch sind — nur `loader.py` und dieser Stub muessen fuer eine
weitere Ziel-DB angefasst werden. Kein Eingriff in Engine, GUI oder CLI.

Was noch fehlt (Buero-Schritte):
  1. Package `oracledb` installieren (Python-Thin-Client, keine Oracle-
     Instant-Client-DLLs noetig): `pip install oracledb`.
  2. Verbindungs-Parameter in Umgebungsvariablen ablegen (nicht in
     der Config-YAML!):
        VIEROL_ORACLE_USER
        VIEROL_ORACLE_PASSWORD
        VIEROL_ORACLE_DSN
  3. In `_verbindung()` die auskommentierten Zeilen aktivieren.
  4. In `lade_oracle()` die TODOs abarbeiten (Tabelle anlegen, INSERT
     bzw. MERGE INTO fuer Upsert-Logik, Transaktions-Handling).
  5. Test-Setup: eine leere Test-Tabelle in der Vierol-Oracle mit den
     erwarteten Spalten anlegen, Prototyp gegen laufen lassen.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from vierol_import.catalog.meta_schema import QuellenConfig
from vierol_import.loading.loader import LadeErgebnis, PKKonfliktFehler
from vierol_import.mapping.mapper import MappingErgebnis

logger = logging.getLogger(__name__)


class OracleNichtVerfuegbar(RuntimeError):
    """Wird geworfen, wenn Oracle-Verbindung noch nicht konfiguriert ist."""


def lade_oracle(
    ergebnis: MappingErgebnis, cfg: QuellenConfig, db_pfad: Path
) -> LadeErgebnis:
    """Wie lade_sqlite, aber zielt auf Oracle statt SQLite.

    Wichtig: `db_pfad` ist bei Oracle bedeutungslos (wir verbinden
    ueber Umgebungsvariablen); das Argument existiert nur, weil die
    Signatur mit sqlite gemeinsam ist.
    """
    # STUB: bis Verbindung/Credentials im Buero da sind, wirft dieser
    # Aufruf eine kontrollierte Exception — die Engine leitet daraus
    # ein sauberes ABGELEHNT_LADEFEHLER ab, das im Audit-Log landet.
    raise OracleNichtVerfuegbar(
        "Oracle-Loader ist noch ein Stub. Bitte im Buero verkabeln "
        "(oracledb installieren + Credentials setzen). "
        "Details siehe Doc-String von 'oracle_loader.py'."
    )

    # -----------------------------------------------------------------
    # AB HIER: der Code, der im Buero aktiviert wird
    # -----------------------------------------------------------------
    #
    # con = _verbindung()
    # try:
    #     _tabelle_anlegen_falls_noetig(con, cfg, ergebnis)
    #
    #     if cfg.zielsystem.pk_konflikt == "reject":
    #         konflikte = _konflikte_zaehlen(con, cfg, ergebnis)
    #         if konflikte > 0:
    #             raise PKKonfliktFehler(konflikte)
    #
    #     geladen, uebersprungen = _saetze_schreiben(con, cfg, ergebnis)
    #     con.commit()
    #     logger.info("Oracle: %s -> %d neu, %d uebersprungen",
    #                 cfg.zielsystem.tabelle, geladen, uebersprungen)
    #     return LadeErgebnis(
    #         tabelle=cfg.zielsystem.tabelle,
    #         zeilen_geladen=geladen,
    #         zeilen_uebersprungen=uebersprungen,
    #     )
    # except Exception:
    #     con.rollback()
    #     raise
    # finally:
    #     con.close()


def _verbindung():
    """Oracle-Verbindung aufbauen. Wird im Buero aktiviert."""
    user = os.environ.get("VIEROL_ORACLE_USER")
    password = os.environ.get("VIEROL_ORACLE_PASSWORD")
    dsn = os.environ.get("VIEROL_ORACLE_DSN")
    if not (user and password and dsn):
        raise OracleNichtVerfuegbar(
            "Umgebungsvariablen VIEROL_ORACLE_USER / _PASSWORD / _DSN "
            "sind nicht gesetzt."
        )

    # import oracledb   # TODO im Buero aktivieren
    # return oracledb.connect(user=user, password=password, dsn=dsn)
    raise OracleNichtVerfuegbar("oracledb-Import noch nicht aktiviert.")


# --- TODOs im Buero abarbeiten: -------------------------------------------
#
# def _tabelle_anlegen_falls_noetig(con, cfg, ergebnis):
#     """CREATE TABLE IF NOT EXISTS equivalent in Oracle."""
#     # Oracle hat kein "IF NOT EXISTS" -> vorher pruefen ob Tabelle da:
#     #   SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(?)
#     # Wenn nicht da: CREATE TABLE mit Spalten aus ergebnis.zielfelder
#     # + passenden Oracle-Typen (VARCHAR2, NUMBER, DATE, ...).
#
# def _saetze_schreiben(con, cfg, ergebnis):
#     """INSERT bzw. MERGE INTO fuer Upsert-Logik."""
#     # Fuer pk_konflikt=skip: INSERT + ORA-00001 abfangen
#     # Fuer pk_konflikt=update: MERGE INTO ... USING dual ON (...)
#     # Fuer pk_konflikt=reject: reiner INSERT (Konflikte vorher gepruefft)
#     # con.executemany(...) fuer Batch-Insert.
#
# def _konflikte_zaehlen(con, cfg, ergebnis):
#     """Vorab-Check: wie viele der einzuspielenden PKs existieren schon?"""
#     # SELECT COUNT(*) FROM tabelle WHERE (pk1,pk2) IN (...)