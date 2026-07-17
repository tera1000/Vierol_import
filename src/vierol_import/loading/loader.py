"""
Load-Layer: schreibt den gemappten DataFrame ins Zielsystem.

Fuer den Prototyp reicht SQLite als Ziel; ueber SQLAlchemy laesst sich
das spaeter mit einer Konfigurationsaenderung auf Oracle, PostgreSQL etc.
umstellen — ohne Codeaenderung. Genau das ist der Punkt.
"""

import logging
from typing import Any

import pandas as pd
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# Fuer den Prototyp: eine lokale SQLite-Datenbank pro Lauf.
# TODO spaeter aus einer zentralen Verbindungs-Konfiguration ableiten.
DEFAULT_SQLITE_URI = "sqlite:///data/target.db"


def load(df: pd.DataFrame, zielsystem_config: dict[str, Any]) -> int:
    """
    DataFrame in die Zieltabelle schreiben.

    TODO Implementieren:
      1. Ziel-URI aus zielsystem_config lesen (oder DEFAULT_SQLITE_URI verwenden)
      2. Ziel-Tabelle und Schema aus zielsystem_config lesen
      3. Ladestrategie beachten (append / replace / upsert)
      4. Anzahl geladener Zeilen zurueckgeben (fuer die Erfolgspruefung)

    Grobskizze:

        engine = create_engine(zielsystem_config.get("uri", DEFAULT_SQLITE_URI))
        strategy = zielsystem_config.get("load_strategie", "append")
        df.to_sql(
            zielsystem_config["tabelle"],
            engine,
            if_exists=strategy,
            index=False,
        )
        return len(df)
    """
    logger.warning("load() ist noch nicht implementiert.")
    return 0
