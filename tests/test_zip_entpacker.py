"""Tests fuer den rekursiven ZIP-Entpacker."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from vierol_import.ingest.zip_entpacker import (
    ZipEntpackfehler,
    entpacke_rekursiv,
)


def test_flaches_zip(tmp_path: Path) -> None:
    """Ein normales ZIP mit zwei Dateien direkt drin."""
    quell_zip = tmp_path / "lieferung.zip"
    with zipfile.ZipFile(quell_zip, "w") as z:
        z.writestr("a.csv", "spalte1;spalte2\nx;y\n")
        z.writestr("b.csv", "andere;datei\n1;2\n")

    ergebnis = entpacke_rekursiv(quell_zip, tmp_path / "entpackt")

    assert len(ergebnis) == 2
    namen = sorted(e.original_pfad for e in ergebnis)
    assert namen == ["a.csv", "b.csv"]
    # Inhalt korrekt entpackt?
    a = next(e for e in ergebnis if e.original_pfad == "a.csv")
    assert a.pfad.read_text().startswith("spalte1;spalte2")


def test_verschachteltes_zip(tmp_path: Path) -> None:
    """ZIP-in-ZIP: inneres ZIP wird transparent mitentpackt."""
    inner_zip = tmp_path / "innen.zip"
    with zipfile.ZipFile(inner_zip, "w") as z:
        z.writestr("inner.csv", "inner content\n")

    aussen_zip = tmp_path / "aussen.zip"
    with zipfile.ZipFile(aussen_zip, "w") as z:
        z.write(inner_zip, "innen.zip")
        z.writestr("aussen.csv", "aussen content\n")

    ergebnis = entpacke_rekursiv(aussen_zip, tmp_path / "entpackt")

    pfade = sorted(e.original_pfad for e in ergebnis)
    assert pfade == ["aussen.csv", "innen.zip/inner.csv"]


def test_zip_slip_wird_abgewiesen(tmp_path: Path) -> None:
    """Boesartiges ZIP mit '../' im Pfad muss abgelehnt werden."""
    boeses_zip = tmp_path / "boese.zip"
    with zipfile.ZipFile(boeses_zip, "w") as z:
        z.writestr("../../etc/passwd", "root:x:0:0:...\n")

    with pytest.raises(ZipEntpackfehler, match="Zip-Slip"):
        entpacke_rekursiv(boeses_zip, tmp_path / "entpackt")


def test_kaputtes_zip(tmp_path: Path) -> None:
    """Datei mit ZIP-Endung, aber kaputtem Inhalt."""
    kaputt = tmp_path / "kaputt.zip"
    kaputt.write_bytes(b"das ist kein zip")

    with pytest.raises(ZipEntpackfehler):
        entpacke_rekursiv(kaputt, tmp_path / "entpackt")


def test_leeres_zip_ist_ok(tmp_path: Path) -> None:
    leer = tmp_path / "leer.zip"
    with zipfile.ZipFile(leer, "w"):
        pass  # nichts drin

    ergebnis = entpacke_rekursiv(leer, tmp_path / "entpackt")
    assert ergebnis == []