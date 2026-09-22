"""
Zentraler Datei-Format-Reader.

Liegt bewusst im catalog-Paket (Wilsons Wunsch), heisst aber
`datei_reader.py` statt `reader.py` — das Modul `catalog/reader.py`
existiert bereits und laedt den YAML-Konfigurationskatalog
(`load_catalog`). Zwei Module mit demselben Namen im selben Paket
waeren ein Namenskonflikt; dieses Modul hat daher einen eigenen,
eindeutigen Namen. Fachlich haben beide Module nichts miteinander zu
tun — `catalog/reader.py` liest Config-YAMLs, dieses Modul liest die
eigentlichen Roh-Datendateien (CSV/Excel/XML/JSON).

Alle Verarbeitungsstufen (Validierung, Mapping, Klassifikation) nutzen
diese eine Funktion — statt selbst je nach Format zu unterscheiden.
Das ist die Format-Abstraktion aus Tabelle 4.1 in der BA (Kapitel 4.2).

--- Hybrid-Backend: bewusste Entscheidung, KEIN einheitliches pandas ---

Fuer CSV/TXT-Dateien wird die Python-Standardbibliothek `csv`
verwendet, NICHT pandas. Grund: pandas' read_csv erzwingt eine
rechteckige Tabellenstruktur — Zeilen mit abweichender Feldanzahl
werden automatisch mit NaN aufgefuellt, statt als strukturelle
Anomalie erkennbar zu bleiben. Genau diese Erkennung ("Spaltenanzahl
X statt Y") ist aber eine funktionale Kernanforderung sowohl der
Validierung (FA-34, zeilenweise Pruefung) als auch der Klassifikation
(strukturelles K.O.-Kriterium, Abschnitt 4.4.1 der BA). Mit pandas
allein wuerde diese Faehigkeit ersatzlos wegfallen.

Fuer Excel (.xlsx/.xlsm/.xls), XML und JSON wird pandas verwendet,
weil diese Formate von Natur aus bereits rechteckig/schema-gebunden
sind (feste Zellen, definierte Elemente) — dort entsteht der oben
beschriebene Informationsverlust nicht, und pandas bringt echten
Mehrwert (ein Werkzeug fuer drei sehr unterschiedliche Binaer-
/Markup-Formate statt drei separate Bibliotheken).

Diese Aufteilung ist selbst wieder ein Beispiel fuer Prinzip 2
(Ports and Adapters, Abschnitt 4.1): der Verarbeitungskern
(Validierung/Mapping/Klassifikation) kennt nur den einheitlichen
Zeilen-Iterator; WELCHES Backend dahinter das konkrete Format liest,
ist fuer ihn unsichtbar und pro Format frei austauschbar.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterator

from vierol_import.catalog.meta_schema import DateiConfig
from vierol_import.encoding_erkennung import erkenne_encoding

logger = logging.getLogger(__name__)


def zeilen_iter(
    pfad: Path, cfg_datei: DateiConfig,
) -> Iterator[tuple[int, list[str]]]:
    """Liest eine Datei und liefert einen Iterator ueber die Zeilen.

    Rueckgabe pro Zeile: (zeilennummer, [feld_werte_als_string]).
    Die Zeilennummer ist 1-basiert; bei Dateien mit Header startet die
    erste Datenzeile bei 2 (analog zu csv.reader-Konvention und
    Editor-Zeilenzaehlung).

    Format-Weiche: CSV/TXT laeuft ueber csv.reader (exakte
    Feldanzahl pro Zeile bleibt erhalten — wichtig fuer strukturelle
    Fehlererkennung). Excel/XML/JSON laufen ueber pandas.
    """
    format = _erkenne_format(pfad, cfg_datei.format)

    if format == "csv":
        yield from _zeilen_iter_csv(pfad, cfg_datei)
    else:
        yield from _zeilen_iter_pandas(pfad, cfg_datei, format)


def _erkenne_format(pfad: Path, cfg_format: str) -> str:
    """Ermittelt das Datei-Format.

    Bei cfg_format=='auto' anhand der Dateiendung. Sonst wird der
    explizit gesetzte Wert genommen und die Erkennung uebersprungen —
    nuetzlich wenn die Endung nicht zum tatsaechlichen Format passt.
    """
    if cfg_format != "auto":
        return cfg_format

    ext = pfad.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return "excel"
    if ext == ".xml":
        return "xml"
    if ext in (".json", ".jsonl"):
        return "json"
    # Alles andere (.csv, .txt, .tsv, .dat, ohne Endung, ...) -> CSV
    return "csv"


# --- CSV/TXT: stdlib csv.reader ---------------------------------------------


def _zeilen_iter_csv(
    pfad: Path, cfg_datei: DateiConfig,
) -> Iterator[tuple[int, list[str]]]:
    """CSV/TXT-Zeilen ueber die Python-Standardbibliothek lesen.

    Bewusst KEIN pandas hier — siehe Modul-Docstring. Jede Zeile wird
    exakt so weitergegeben, wie der Trennzeichen-Split sie ergibt,
    inklusive abweichender Feldanzahl bei strukturell kaputten Zeilen.
    """
    encoding = erkenne_encoding(pfad, wunsch=cfg_datei.encoding)
    with open(pfad, newline="", encoding=encoding) as f:
        # quoting-Modus aus der Config uebernehmen — steuert wie der
        # CSV-Parser mit dem Anfuehrungszeichen '"' umgeht.
        #
        # 'minimal' (Default): klassisches CSV — '"' umschliesst Felder
        #   als String-Delimiter. Passt fuer Formate wie TecDoc.
        # 'none': '"' wird als gewoehnliches Zeichen behandelt. Passt
        #   fuer Vierol-Pipe-Dateien mit '"' in Freitextwerten.
        #
        # Empirisch gefundenes Problem, das dieses Feld loest: manche
        # Vierol-Datenlieferungen (z.B. Subaru, BMW) enthalten in
        # Freitextfeldern (BEZEICHNUNG) ein woertliches '"' als Teil
        # des Werts, z.B. NLA"GOLF TEES. Mit CSV-Standardverhalten
        # (minimal) interpretiert csv.reader das '"' als Beginn eines
        # gequoteten Felds und liest alles bis zum naechsten '"' als
        # EIN Feld ein — inklusive Zeilenumbruechen und nachfolgender
        # Datensaetze. Symptome:
        #   * Absurd lange Werte (18.000+ Zeichen) -> ORA-12899 beim
        #     Laden
        #   * Bei extremen Faellen ueberschreitet das "Feld" die
        #     Python-interne csv.field_size_limit (131.072 Zeichen)
        #     und die GESAMTE Datei bricht mit einem Python-Error ab
        # Mit quoting='none' fuer diese Quellen wird der Effekt
        # vermieden.
        quoting_flag = (
            csv.QUOTE_NONE if cfg_datei.quoting == "none"
            else csv.QUOTE_MINIMAL
        )
        reader = csv.reader(
            f, delimiter=cfg_datei.trennzeichen, quoting=quoting_flag,
        )
        start_zeile = 2 if cfg_datei.hat_header else 1
        if cfg_datei.hat_header:
            next(reader, None)
        for nr, zeile in enumerate(reader, start=start_zeile):
            yield nr, zeile


# --- Excel/XML/JSON: pandas ---------------------------------------------


def _zeilen_iter_pandas(
    pfad: Path, cfg_datei: DateiConfig, format: str,
) -> Iterator[tuple[int, list[str]]]:
    """Excel/XML/JSON ueber pandas lesen.

    Diese Formate sind von Natur aus rechteckig/schema-gebunden, daher
    entsteht hier NICHT das Problem, das pandas fuer CSV ungeeignet
    macht (siehe Modul-Docstring).
    """
    df = _lade_dataframe(pfad, cfg_datei, format)
    # Leere Zellen zu leerem String statt NaN — konsistent zum
    # csv.reader-Verhalten.
    df = df.fillna("")

    # Bei Excel/XML/JSON gibt es immer eine "Header-Ebene" (Feldnamen /
    # Elementstruktur). Zaehlung startet bei 2 wenn hat_header gesetzt
    # ist, sonst bei 1 — konsistent zur CSV-Zaehlung.
    start = 2 if cfg_datei.hat_header else 1

    for i, row in enumerate(df.itertuples(index=False, name=None)):
        felder = [_normalisiere_wert(v) for v in row]
        yield start + i, felder


def _normalisiere_wert(v) -> str:
    """Wandelt einen pandas-Wert in einen sauberen String.

    NaN, None und leere Werte werden zu "" — konsistent zum
    csv.reader-Verhalten. Zahlen ohne Nachkommastelle bleiben ohne
    ".0" (12.0 -> "12"), damit die Validierung nicht anschliessend
    an nicht-passenden Typen scheitert.
    """
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN ist nie gleich sich selbst
            return ""
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v)


def _lade_dataframe(pfad: Path, cfg_datei: DateiConfig, format: str):
    """Formatspezifischer pandas-Aufruf fuer Excel/XML/JSON.

    dtype=str verhindert automatische Typinferenz — die Konvertierung
    passiert kontrolliert weiter unten in typen.konvertiere() nach den
    Regeln aus der Config.
    """
    import pandas as pd

    if format == "excel":
        try:
            return pd.read_excel(
                pfad,
                sheet_name=cfg_datei.sheet if cfg_datei.sheet else 0,
                header=0 if cfg_datei.hat_header else None,
                dtype=str,
                keep_default_na=False,
                na_values=[],
            )
        except ValueError as e:
            # leeres oder kaputtes Sheet etc.
            logger.warning("Excel-Datei %s nicht lesbar: %s", pfad.name, e)
            return pd.DataFrame()

    if format == "xml":
        try:
            return pd.read_xml(
                pfad,
                xpath=cfg_datei.xpath if cfg_datei.xpath else "./*",
                dtype=str,
            )
        except ValueError as e:
            logger.warning("XML-Datei %s nicht lesbar: %s", pfad.name, e)
            return pd.DataFrame()

    if format == "json":
        try:
            return pd.read_json(
                pfad,
                orient=cfg_datei.json_orient,
                lines=cfg_datei.json_lines,
                dtype=str,
            )
        except ValueError as e:
            logger.warning("JSON-Datei %s nicht lesbar: %s", pfad.name, e)
            return pd.DataFrame()

    raise ValueError(
        f"Unbekanntes Datei-Format '{format}' fuer pandas-Reader. "
        f"Erlaubt: excel, xml, json."
    )