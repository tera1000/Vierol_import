"""
Tests fuer das Ingest-Modul: Verzeichnisscan, Archivierung, Reject-
Berichte und Verzeichnis-Scan.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path


from vierol_import.ingest.watcher import (
    scanne_ingest,
    verschiebe_ins_archiv,
    verschiebe_ins_reject,
)


def test_scan_findet_dateien(tmp_path: Path) -> None:
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    (ingest / "a.csv").write_text("x", encoding="utf-8")
    (ingest / "b.txt").write_text("y", encoding="utf-8")
    (ingest / ".versteckt").write_text("z", encoding="utf-8")  # ignoriert

    dateien = scanne_ingest(ingest)
    namen = sorted(p.name for p in dateien)
    assert namen == ["a.csv", "b.txt"]


def test_scan_nicht_existierendes_verzeichnis(tmp_path: Path) -> None:
    assert scanne_ingest(tmp_path / "gibts_nicht") == []


def test_archivierung_verschiebt_und_stempelt(tmp_path: Path) -> None:
    datei = tmp_path / "test.csv"
    datei.write_text("data", encoding="utf-8")
    archive = tmp_path / "archive"

    zeit = datetime(2026, 7, 17, 10, 30, 45)
    ziel = verschiebe_ins_archiv(datei, archive, "meine_quelle", zeitstempel=zeit)

    assert not datei.exists()
    assert ziel.exists()
    assert ziel.parent.name == "meine_quelle"
    assert "20260717_103045" in ziel.name
    assert ziel.suffix == ".csv"


def test_reject_erstellt_bericht(tmp_path: Path) -> None:
    datei = tmp_path / "kaputt.csv"
    datei.write_text("data", encoding="utf-8")
    reject = tmp_path / "reject"

    zeit = datetime(2026, 7, 17, 12, 0, 0)
    ziel = verschiebe_ins_reject(
        datei, reject, "Kein Match", ["Detail 1", "Detail 2"], zeitstempel=zeit
    )

    bericht = ziel.with_suffix(ziel.suffix + ".reject.txt")
    assert bericht.exists()
    text = bericht.read_text(encoding="utf-8")
    assert "kaputt.csv" in text
    assert "Kein Match" in text
    assert "Detail 1" in text
    assert "Detail 2" in text