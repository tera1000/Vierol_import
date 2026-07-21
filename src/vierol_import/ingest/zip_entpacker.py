"""
Rekursiver ZIP-Entpacker fuer Lieferdateien vom Dienstleister.

Manche Lieferungen kommen als geschachtelte ZIPs:
  lieferung_juli.zip
  ├── preise.zip
  │   ├── us_oe.csv
  │   └── de_oe.csv
  ├── bestand.csv
  └── stammdaten.zip
      └── stammdaten.txt

Nach dem Entpacken bekommt der Aufrufer eine flache Liste ALLER
Enddateien (die vier oben), zusammen mit ihrem urspruenglichen
Pfad im ZIP (zur Anzeige).

Sicherheit:
  * Zip-Slip-Schutz: entpackte Dateipfade duerfen den Zielordner
    nicht verlassen. Ein boesartiges ZIP mit "../../etc/passwd"
    wird abgewiesen.
  * ZIP-Bomben-Schutz: Gesamtgroesse aller entpackten Bytes wird
    begrenzt (MAX_ENTPACKT). Verhindert, dass ein 1-MB-ZIP zu
    100 GB expandiert.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 5 GB nach Entpacken als harte Obergrenze fuer einen einzelnen Upload.
# Anpassbar, wenn echte Lieferungen groesser werden.
MAX_ENTPACKT_BYTES = 5 * 1024 * 1024 * 1024

MAX_TIEFE = 5   # Schutz vor unendlicher ZIP-in-ZIP-Rekursion


class ZipEntpackfehler(Exception):
    """Fehler beim (rekursiven) Entpacken eines ZIPs — z. B. Groessen-
    Ueberschreitung, Zip-Slip-Versuch, kaputte Archivdatei."""


@dataclass
class EntpackteDatei:
    """Eine einzelne, nutzbare Datei aus dem (rekursiv entpackten) ZIP."""

    pfad: Path            # tatsaechlicher Pfad im Temp-Verzeichnis
    original_pfad: str    # der Pfad im ZIP-Baum ("preise.zip/us_oe.csv")
    groesse: int


def entpacke_rekursiv(
    zip_pfad: Path, ziel_dir: Path, _tiefe: int = 0, _bytes_bisher: int = 0
) -> list[EntpackteDatei]:
    """Ein ZIP komplett entpacken; enthaltene ZIPs auch entpacken.

    Gibt eine flache Liste aller Enddateien zurueck (keine ZIPs mehr).
    """
    if _tiefe > MAX_TIEFE:
        raise ZipEntpackfehler(
            f"ZIP-Verschachtelung zu tief (>{MAX_TIEFE} Ebenen) — verdaechtig."
        )

    ziel_dir.mkdir(parents=True, exist_ok=True)
    ergebnisse: list[EntpackteDatei] = []
    bytes_hier = _bytes_bisher

    try:
        with zipfile.ZipFile(zip_pfad, "r") as archiv:
            for info in archiv.infolist():
                if info.is_dir():
                    continue

                # Zip-Slip-Schutz: entpackter Pfad muss innerhalb ziel_dir bleiben
                ziel_datei = (ziel_dir / info.filename).resolve()
                if not str(ziel_datei).startswith(str(ziel_dir.resolve())):
                    raise ZipEntpackfehler(
                        f"Verdaechtiger Pfad im ZIP: '{info.filename}' "
                        "(Zip-Slip-Versuch)"
                    )

                # ZIP-Bomben-Schutz
                bytes_hier += info.file_size
                if bytes_hier > MAX_ENTPACKT_BYTES:
                    raise ZipEntpackfehler(
                        f"Entpackt-Gesamtgroesse ueberschreitet "
                        f"{MAX_ENTPACKT_BYTES / (1024**3):.1f} GB — verdaechtig."
                    )

                ziel_datei.parent.mkdir(parents=True, exist_ok=True)
                with archiv.open(info) as quelle, open(ziel_datei, "wb") as ziel:
                    ziel.write(quelle.read())

                # Verschachteltes ZIP? Rekursion.
                if _ist_zip(ziel_datei):
                    inner_dir = ziel_datei.with_suffix(ziel_datei.suffix + "_entpackt")
                    unter = entpacke_rekursiv(
                        ziel_datei, inner_dir, _tiefe + 1, bytes_hier
                    )
                    # Original-Pfad-Praefix ergaenzen, damit klar ist,
                    # aus welchem inneren ZIP die Datei kam.
                    for u in unter:
                        u.original_pfad = f"{info.filename}/{u.original_pfad}"
                    ergebnisse.extend(unter)
                    # Bytes von rekursiv entpackten Dateien mitzaehlen
                    bytes_hier += sum(u.groesse for u in unter)
                else:
                    ergebnisse.append(
                        EntpackteDatei(
                            pfad=ziel_datei,
                            original_pfad=info.filename,
                            groesse=info.file_size,
                        )
                    )
    except zipfile.BadZipFile as e:
        raise ZipEntpackfehler(f"Kaputte ZIP-Datei: {e}") from e

    logger.info(
        "ZIP entpackt: %s -> %d Datei(en), %.2f MB gesamt",
        zip_pfad.name, len(ergebnisse), sum(e.groesse for e in ergebnisse) / (1024**2),
    )
    return ergebnisse


def _ist_zip(pfad: Path) -> bool:
    """Zuverlaessige ZIP-Erkennung anhand des Datei-Headers (Magic Bytes)
    statt nur ueber die Dateiendung — manche ZIPs heissen `.dat` o.ae."""
    try:
        with open(pfad, "rb") as f:
            head = f.read(4)
        # ZIP-Magic-Bytes: PK\x03\x04 (Local File Header) oder PK\x05\x06 (leeres ZIP)
        return head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06")
    except OSError:
        return False