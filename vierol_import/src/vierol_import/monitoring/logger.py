"""
Zentrales Logging-Setup.

Design-Entscheidung: Datei-Log wurde durch die Audit-Tabelle in der
SQLite-DB ersetzt (`audit_log.py`). Das reine Python-Logging schreibt
nur noch in die Konsole und dient dem Debugging / operativen Feedback,
NICHT der dauerhaften Protokollierung.

Warum: Das Datei-Log war schwer auswertbar (Textzeilen, keine Filter),
und der Fachbereich hat Reporting-Bedarf. Eine SQLite-Tabelle ist
auswertbar per SQL / Excel / Power BI.

Wer den Verlauf aufrufen will: `python -m vierol_import zeige-log`
oder direkt in der DB die Tabelle `import_lauf` abfragen.
"""

from __future__ import annotations

import logging


def setup_logging(verbose: bool = False) -> None:
    """Konsolen-Logging einrichten.

    Ohne `verbose`: nur WARNING/ERROR sichtbar (ruhige Konsole).
    Mit `verbose`: alle Pipeline-Schritte sichtbar (Debug-Sitzung).
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Falls doppelt initialisiert (z.B. Streamlit-Rerun): saeubern.
    for h in list(root.handlers):
        root.removeHandler(h)

    konsole = logging.StreamHandler()
    konsole.setLevel(logging.DEBUG if verbose else logging.WARNING)
    konsole.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(konsole)