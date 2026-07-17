"""
Zentrales Logging-Setup.

Grundsatz: Das CLI kommuniziert mit dem User ueber click.echo (bewusst
gestaltete Ausgabe), das Logging dient der NACHVOLLZIEHBARKEIT
(Kontrolle-Stufe der Architektur). Im Normalbetrieb bleibt die Konsole
daher ruhig (nur Warnungen); mit --verbose wird jeder Pipeline-Schritt
sichtbar. Zusaetzlich schreibt ein File-Handler alle Details in eine
Logdatei — die Basis fuer den spaeteren Lauf-Report.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG_DATEI = Path("data/import.log")


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    konsole = logging.StreamHandler()
    konsole.setLevel(logging.DEBUG if verbose else logging.WARNING)
    konsole.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(konsole)

    try:
        LOG_DATEI.parent.mkdir(parents=True, exist_ok=True)
        datei = logging.FileHandler(LOG_DATEI, encoding="utf-8")
        datei.setLevel(logging.DEBUG)
        datei.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(datei)
    except OSError:
        root.warning("Logdatei %s nicht schreibbar — nur Konsolen-Logging.", LOG_DATEI)