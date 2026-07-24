"""
Automatische Encoding-Erkennung fuer Dateien vom Dienstleister.

Kontext: Die Configs geben ein Wunsch-Encoding vor (z. B. utf-8), aber
die tatsaechlichen Lieferdateien haben oft ein anderes. Excel exportiert
gerne cp1252 (Windows-Latin-1), aeltere Systeme iso-8859-1, moderne
UTF-8 mit BOM. Statt jeden Import an einem Encoding-Fehler scheitern
zu lassen, probieren wir eine Kaskade.

Vorgehen:
  1. Wenn die Config explizit ein Encoding vorgibt (nicht der Default
     "utf-8"), wird ausschliesslich das versucht.
  2. Andernfalls: eine kleine, geordnete Liste probieren; das erste,
     das die Datei komplett lesen kann, gewinnt.

Das Ergebnis wird ins Log geschrieben, damit im Betrieb nachvollziehbar
bleibt, welches Encoding fuer eine Datei erkannt wurde.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Reihenfolge ist wichtig: UTF-8 zuerst (moderner Standard),
# dann UTF-8 mit BOM, dann die haeufigsten Windows-Encodings.
# Latin-1 ganz am Ende als "kann alles lesen"-Notausgang, weil es
# jedes Byte akzeptiert (auch falsch interpretiert).
FALLBACK_KETTE = ["utf-8", "utf-8-sig", "cp1252", "iso-8859-1", "latin-1"]


def erkenne_encoding(
    datei: Path, wunsch: str | None = None
) -> str:
    """Findet ein Encoding, mit dem die Datei komplett lesbar ist.

    Wenn `wunsch` gesetzt und nicht 'utf-8' ist, wird nur dieses
    Encoding probiert (Nutzer hat sich bewusst festgelegt). Sonst
    laeuft die Fallback-Kaskade.

    Wirft `UnicodeDecodeError`, wenn KEIN Encoding funktioniert —
    das ist bei realen Textdateien praktisch unmoeglich, weil
    latin-1 alle Bytes akzeptiert.
    """
    # Explizite Vorgabe respektieren (z. B. wenn User weiss: cp1252)
    if wunsch and wunsch.lower() != "utf-8":
        _testen(datei, wunsch)
        return wunsch

    for enc in FALLBACK_KETTE:
        try:
            _testen(datei, enc)
            if enc != "utf-8":
                logger.info(
                    "Encoding fuer '%s' erkannt: %s (Fallback aus utf-8)",
                    datei.name, enc,
                )
            return enc
        except UnicodeDecodeError:
            continue

    raise UnicodeDecodeError(
        "utf-8", b"", 0, 1,
        f"Kein Encoding aus der Kaskade konnte '{datei.name}' lesen "
        "(auch nicht latin-1 — Datei ist wahrscheinlich binaer, "
        "keine Textdatei).",
    )


def _testen(datei: Path, encoding: str) -> None:
    """Versucht, die Datei zu lesen. Wirft UnicodeDecodeError bei
    Fehlschlag, sonst kein Rueckgabewert."""
    # Groessere Dateien in Bloecken lesen, damit wir bei mehreren GB
    # nicht alles im RAM haben. 1 MB pro Iteration reicht, um Codec-
    # Probleme zuverlaessig zu erkennen.
    with open(datei, "r", encoding=encoding) as f:
        while f.read(1024 * 1024):
            pass