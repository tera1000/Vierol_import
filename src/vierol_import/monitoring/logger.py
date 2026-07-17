"""
Zentrales Logging-Setup.

Nutzt python-json-logger fuer strukturierte JSON-Ausgabe, damit
Log-Eintraege maschinell auswertbar sind (Grundlage fuer spaeteres
Monitoring). Zusaetzlich lesbare Konsolen-Ausgabe fuer die Entwicklung.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Menschenlesbar auf STDOUT
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(console)

    # JSON-Log in Datei fuer spaetere Auswertung
    json_handler = logging.FileHandler("vierol_import.log.jsonl", encoding="utf-8")
    json_handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    )
    root.addHandler(json_handler)
