"""
Meta-Schema fuer den Konfigurations-Katalog (v2).

Kernannahmen (aus Gespraechen mit dem Fachbereich):
- Dateien kommen in der Regel OHNE Header-Zeile.
- Dateinamen sind KEIN verlaessliches Erkennungsmerkmal (immer anders).
- Stabil pro Quelle sind: Trennzeichen, Spaltenanzahl und die
  Struktur der Spalten (Datentypen / Wertemuster).

Design-Entscheidung: Die Spaltenstruktur wird EINMAL zentral im Block
`spalten` definiert und von drei Pipeline-Stufen gemeinsam genutzt:

  1. Erkennung:   prueft eine Stichprobe der Datei gegen Typ/Muster
                  jeder Spalte -> Score fuer das Vorschlags-Ranking.
  2. Validierung: prueft die GESAMTE Datei gegen dieselben Typen
                  plus Wertebereiche (minimum/maximum, pflicht, max_laenge).
  3. Mapping:     verwendet den logischen Spaltennamen (`name`) als
                  Quellbezug -> die Position steht nur an einer Stelle.

Beispiel einer vollstaendigen Quellen-Config:

    name: topmotive
    beschreibung: "Warenkorbdaten von Topmotive"

    datei:
      trennzeichen: ";"
      encoding: "utf-8"
      hat_header: false

    spalten:
      - {position: 0, name: artikelnummer, typ: string, muster: "^[A-Z]{2}-\\d{5}$"}
      - {position: 1, name: hersteller_id, typ: integer}
      - {position: 2, name: bezeichnung,   typ: string, max_laenge: 350}
      - {position: 3, name: menge,         typ: integer, minimum: 0}
      - {position: 4, name: preis,         typ: decimal_de, minimum: 0}

    klassifikation:
      stichprobe_zeilen: 20
      schwellenwert: 0.9

    mapping:
      regeln:
        - {quelle: artikelnummer, ziel: artikel_id}
        - {quelle: menge,         ziel: menge}
        - {quelle: preis,         ziel: preis_netto}

    zielsystem:
      typ: sqlite
      tabelle: warenkorb_positionen
      upsert_key: [artikel_id]
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Unbekannte Felder in der YAML sind ein Fehler, kein stilles
    Ignorieren — faengt Tippfehler sofort beim validate-config-Lauf."""

    model_config = ConfigDict(extra="forbid")


# --- Datei-Eigenschaften -----------------------------------------------------


class DateiConfig(StrictModel):
    """Physikalische Eigenschaften der Datei. Trennzeichen und Encoding
    sind pro Quelle stabil und dienen als K.O.-Kriterium der Erkennung.

    Format-Steuerung: mit 'auto' (Default) wird das Format anhand der
    Dateiendung erkannt (.xlsx/.xlsm/.xls -> excel, .xml -> xml,
    .json/.jsonl -> json, sonst csv). Explizite Werte 'csv', 'excel',
    'xml' oder 'json' erzwingen das Format unabhaengig von der Endung.
    """

    format: Literal["auto", "csv", "excel", "xml", "json"] = Field(
        default="auto",
        description=(
            "Datei-Format. 'auto' (Default) erkennt anhand der "
            "Dateiendung. Explizit setzen wenn die Endung nicht zum "
            "tatsaechlichen Format passt."
        ),
    )
    # CSV/TXT-spezifisch (bei Excel/XML/JSON ignoriert)
    trennzeichen: str = ";"
    encoding: str = "utf-8"
    hat_header: bool = False
    # Excel-spezifisch
    sheet: str | None = Field(
        default=None,
        description=(
            "Nur fuer format=excel: Name der Registerkarte (Tab). "
            "Wenn leer, wird die erste Registerkarte genommen."
        ),
    )
    # XML-spezifisch
    xpath: str | None = Field(
        default=None,
        description=(
            "Nur fuer format=xml: XPath-Ausdruck der die Elemente "
            "adressiert, die als Zeilen behandelt werden. Wenn leer, "
            "nutzt pandas den Standard './*' (direkte Kinder der Wurzel)."
        ),
    )
    # JSON-spezifisch
    json_orient: str = Field(
        default="records",
        description=(
            "Nur fuer format=json: pandas-Orientierung. 'records' "
            "erwartet ein Array von Objekten [{...}, {...}]. Andere "
            "Werte: 'columns', 'index', 'split', 'table', 'values' — "
            "siehe pandas.read_json-Dokumentation."
        ),
    )
    json_lines: bool = Field(
        default=False,
        description=(
            "Nur fuer format=json: True fuer JSONL (eine Zeile = ein "
            "JSON-Objekt). Bei json_lines=True wird json_orient ignoriert."
        ),
    )
    # CSV-Quoting-Verhalten (nur fuer format=csv relevant)
    quoting: Literal["minimal", "none"] = Field(
        default="minimal",
        description=(
            "Wie der CSV-Parser mit Anfuehrungszeichen ('\"') umgeht.\n"
            "\n"
            "  'minimal' (Default): klassisches CSV-Verhalten — '\"' "
            "umschliesst Felder als String-Delimiter. Passt fuer "
            "Formate wie TecDoc, die jedes Feld gequotet ausgeben:\n"
            "     \"OPEL\";\"Corsa\";\"Passenger cars\";...\n"
            "\n"
            "  'none': '\"' wird als GEWOEHNLICHES Zeichen behandelt, "
            "kein Feld-Verketten. Passt fuer Formate ohne CSV-Quoting, "
            "die '\"' als Bestandteil von Freitext enthalten koennen — "
            "z.B. die Vierol-Pipe-Dateien:\n"
            "     Isuzu|X132|NLA\"GOLF TEES/MARKER KIT-NLA|...\n"
            "\n"
            "Falscher Wert fuehrt zu subtilen Datenqualitaets-Problemen: "
            "  * minimal + '\"'-Zeichen im Freitext -> Zeilen werden "
            "    ueber Zeilenumbrueche hinweg verkettet, Werte werden "
            "    absurd lang (Symptome: ORA-12899, 'field larger than "
            "    field limit')\n"
            "  * none + echtes CSV-Quoting -> '\"' bleibt Teil des "
            "    Werts (z.B. Feld wird '\"OPEL' statt 'OPEL')"
        ),
    )


# --- Zentrale Spaltendefinition ---------------------------------------------

SpaltenTyp = Literal[
    "string",       # beliebiger Text
    "integer",      # Ganzzahl, z. B. "42"
    "decimal_de",   # deutsches Dezimalformat, z. B. "1.234,56"
    "decimal_en",   # englisches Dezimalformat, z. B. "1234.56"
    "date_dmy",     # Datum TT.MM.JJJJ
    "date_iso",     # Datum JJJJ-MM-TT
    "boolean",      # 0/1, ja/nein, true/false
]


class SpaltenDef(StrictModel):
    """Definition genau einer Spalte der Quelldatei.

    `position` ist der 0-basierte Spaltenindex (Dateien haben keinen
    Header, also ist die Position die einzige verlaessliche Adresse).
    `name` ist ein logischer Name, den WIR vergeben — er existiert nur
    in der Config und macht Mapping-Regeln lesbar.
    """

    position: int = Field(..., ge=0)
    name: str = Field(..., min_length=1)
    typ: SpaltenTyp = "string"
    muster: str | None = Field(
        default=None,
        description="Optionales Regex-Muster fuer den Zellinhalt, z. B. '^[A-Z]{2}-\\d{5}$'.",
    )
    pflicht: bool = True
    minimum: float | None = None
    maximum: float | None = None
    max_laenge: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optionale maximale Zeichenlaenge des Zellwerts. Nuetzlich, um "
            "Ziel-DB-Spaltenlaengen zu spiegeln (z.B. Oracle VARCHAR2(350)) "
            "und zu lange Werte schon in der Validierung als Quarantaene-"
            "Zeile zu erkennen, statt sie erst beim DB-Schreibvorgang "
            "scheitern zu lassen (z.B. ORA-12899: value too large for column)."
        ),
    )


# --- Klassifikation (Erkennung) ----------------------------------------------


class KlassifikationConfig(StrictModel):
    """Parameter fuer die inhaltsbasierte Erkennung.

    K.O.-Kriterien (nicht konfigurierbar, ergeben sich aus `datei` und
    `spalten`): Datei laesst sich mit dem Trennzeichen parsen und hat
    exakt die erwartete Spaltenanzahl.

    Fein-Score: Anteil der Zellen in der Stichprobe, die zu Typ und
    Muster ihrer Spaltendefinition passen.
    """

    stichprobe_zeilen: int = Field(default=20, ge=1)
    schwellenwert: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Ab diesem Score gilt eine Quelle als sicherer Vorschlag.",
    )


# --- Mapping ------------------------------------------------------------------


class MappingRegel(StrictModel):
    quelle: str = Field(
        ..., description="Logischer Spaltenname aus dem `spalten`-Block."
    )
    ziel: str = Field(..., description="Feldname im kanonischen Datenmodell.")
    konvertierung: str | None = Field(
        default=None,
        description=(
            "Optionaler zusaetzlicher Konvertierungs-Key. Die Typ-Konvertierung "
            "(decimal_de -> float etc.) folgt bereits aus dem Spaltentyp."
        ),
    )


class AbgeleitetesFeld(StrictModel):
    """Zielfeld, das NICHT aus einer Dateispalte kommt, sondern beim
    Import berechnet wird. Beispiel: `jahrmonat` aus dem Ladezeitpunkt.

    `funktion` referenziert eine registrierte Funktion in der
    Mapping-Engine — neue Ableitungen werden dort einmal implementiert
    und sind dann fuer alle Quellen per YAML nutzbar."""

    ziel: str = Field(..., min_length=1)
    funktion: Literal["ladezeitpunkt_jahrmonat", "ladezeitpunkt_datum", "dateiname"]


# --- Neue vereinheitlichte Feld-Struktur (Briefing-konform) -----------------


FeldKategorie = Literal["quelle", "auto", "system", "berechnet"]


class MappingFeld(StrictModel):
    """Vereinheitlichte Zielfeld-Definition mit expliziter Kategorie.

    Die vier Kategorien aus dem Briefing:
      - quelle: Wert kommt direkt aus einer Quellspalte (via `von`)
      - auto: Wert wird von der Ziel-DB gesetzt (z.B. Auto-Increment).
              Beim Load nicht mitgeschrieben.
      - system: Wert kommt aus dem Verarbeitungskontext
                (Ladezeitpunkt, Dateiname etc. — siehe `system_quelle`)
      - berechnet: Wert wird aus anderen bereits gemappten Feldern
                   ausgewertet (siehe `formel`)
    """

    ziel: str = Field(..., min_length=1, description="Zielspaltenname in der DB")
    kategorie: FeldKategorie

    # Nur bei kategorie=quelle:
    von: str | None = Field(
        default=None,
        description=(
            "Nur bei kategorie=quelle: Name der Quellspalte "
            "(aus dem `spalten`-Block)."
        ),
    )
    typ: str | None = Field(
        default=None,
        description=(
            "Nur bei kategorie=quelle: optionaler Konvertierungs-Hinweis. "
            "Ueblicherweise ist der Typ schon aus der Spaltendefinition bekannt."
        ),
    )

    # Nur bei kategorie=system:
    system_quelle: Literal[
        "ladezeitpunkt", "ladezeitpunkt_jahrmonat",
        "ladezeitpunkt_datum", "dateiname",
        "elternordner", "pfad_regex",
    ] | None = Field(
        default=None,
        description=(
            "Nur bei kategorie=system: aus welchem Kontext-Wert wird das "
            "Feld befuellt.\n"
            "  - ladezeitpunkt: voller Timestamp der Verarbeitung\n"
            "  - ladezeitpunkt_jahrmonat: z.B. '202608'\n"
            "  - ladezeitpunkt_datum: z.B. '2026-08-05'\n"
            "  - dateiname: Basename der Quelldatei\n"
            "  - elternordner: Name des unmittelbaren Elternordners "
            "(z.B. '2026-04-01' aus '.../2026-04-01/daten.csv')\n"
            "  - pfad_regex: Wert aus dem vollen Pfad per Regex extrahiert "
            "(braucht zusaetzlich 'regex' und optional 'gruppe')"
        ),
    )
    regex: str | None = Field(
        default=None,
        description=(
            "Nur bei system_quelle=pfad_regex: Python-Regex mit mindestens "
            "einer Gruppe. Der Match wird auf den vollen Pfad "
            "(als POSIX-String mit Forward-Slashes) angewendet."
        ),
    )
    gruppe: int = Field(
        default=1, ge=1,
        description=(
            "Nur bei system_quelle=pfad_regex: welche Regex-Gruppe verwendet "
            "wird (1-basiert). Default 1 = erste Gruppe."
        ),
    )
    gruppen: list[int] | None = Field(
        default=None,
        description=(
            "Nur bei system_quelle=pfad_regex: Alternative zu 'gruppe' fuer "
            "zusammengesetzte Werte. Mehrere Regex-Gruppen werden ohne "
            "Trennzeichen aneinandergehaengt, z.B. gruppen: [1, 2] mit "
            "regex '(\\d{4})-(\\d{2})-(\\d{2})' ergibt aus '2026-04-01' den "
            "Wert '202604'. Wenn gesetzt, hat 'gruppen' Vorrang vor 'gruppe'."
        ),
    )

    # Nur bei kategorie=berechnet:
    formel: str | None = Field(
        default=None,
        description=(
            "Nur bei kategorie=berechnet: einfacher arithmetischer Ausdruck "
            "ueber bereits gemappte Felder, z.B. 'menge * preis_netto'. "
            "Nur numerische Operatoren + - * / und Klammern erlaubt."
        ),
    )


class MappingConfig(StrictModel):
    # ALT: getrennt regeln + abgeleitete_felder — bleibt fuer Rueckwaerts-
    # Kompatibilitaet der 3 existierenden Configs.
    regeln: list[MappingRegel] = Field(default_factory=list)
    abgeleitete_felder: list[AbgeleitetesFeld] = Field(default_factory=list)

    # NEU: vereinheitlichte Feld-Liste mit Kategorien. Wenn `felder`
    # gesetzt ist, wird sie bevorzugt und die alten Felder ignoriert.
    felder: list[MappingFeld] = Field(default_factory=list)

    @field_validator("regeln")
    @classmethod
    def _regeln_kein_ziel_doppelt(cls, v: list[MappingRegel]) -> list[MappingRegel]:
        # Die alte "mindestens_eine_regel"-Regel wurde entschaerft:
        # jetzt darf `regeln` leer sein, WENN stattdessen `felder`
        # gesetzt ist. Die Pruefung findet in _mapping_hat_mindestens_ein_ziel
        # statt (model_validator, sieht alle Felder).
        return v

    @model_validator(mode="after")
    def _mapping_hat_mindestens_ein_ziel(self) -> "MappingConfig":
        """Entweder `felder` (neu) oder `regeln` (alt) muss Ziele enthalten.

        Wenn beides leer ist, waere das Mapping-Ergebnis leer und die
        Pipeline waere sinnlos.
        """
        if not self.felder and not self.regeln:
            raise ValueError(
                "mapping braucht mindestens ein Ziel: entweder in "
                "`felder` (neuer Weg) oder in `regeln` (alter Weg)."
            )
        return self


# --- Zielsystem (Load) --------------------------------------------------------


class ZielsystemConfig(StrictModel):
    """Referenz auf eine Verbindung + Ziel-Tabelle.

    Vor der Umstrukturierung stand hier direkt `typ: sqlite` bzw. `oracle`.
    Ab jetzt zeigt `ref` auf eine Verbindung in `zielsysteme/*.yaml` — der
    Typ, die Credentials und alle Verbindungsdetails leben dort. Dadurch:
      * Passwoerter niemals in der Quellen-YAML,
      * einfacher Wechsel Test / Produktiv (nur `ref` austauschen),
      * mehrere Verbindungen desselben Typs moeglich.
    """

    ref: str = Field(
        description=(
            "Name der Zielsystem-Verbindung (Datei zielsysteme/<name>.yaml)."
        )
    )
    tabelle: str = Field(
        description="Zieltabelle in dem referenzierten System."
    )
    upsert_key: list[str] = Field(default_factory=list)
    pk_konflikt: Literal["skip", "update", "insert"] = Field(
        default="skip",
        description=(
            "Verhalten bei bereits existierendem Primaerschluessel:\n"
            "  - skip   (Default): neuen Datensatz einspielen, alten behalten & ueberspringen\n"
            "  - update: neuen Datensatz einspielen, alten ueberschreiben\n"
            "  - insert: ganze Datei ablehnen, wenn auch nur ein Konflikt auftritt"
        ),
    )
    fehler_modus: Literal["alles_oder_nichts", "partiell"] = Field(
        default="alles_oder_nichts",
        description=(
            "Verhalten bei fehlerhaften Zeilen in der Datei:\n"
            "  - alles_oder_nichts (Default): eine kaputte Zeile -> Datei komplett\n"
            "    abgelehnt, keine Daten geladen. Sicher, aber im Alltag oft "
            "    unpraktisch bei grossen Dateien mit vereinzelten Fehlern.\n"
            "\n"
            "HINWEIS: Wenn fehler_schwelle gesetzt ist, wird sie bevorzugt "
            "und dieses Feld ignoriert."
        ),
    )
    fehler_schwelle: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Feiner steuerbare Fehlerquote (0.0 - 1.0). Wenn gesetzt, "
            "wird sie an Stelle von fehler_modus verwendet:\n"
            "  - 0.0: keine Fehler tolerieren (= alles_oder_nichts)\n"
            "  - 0.05: bis 5% fehlerhafte Zeilen ok, dann Datei komplett ablehnen\n"
            "  - 1.0: alle Fehler tolerieren (= partiell)\n"
            "\n"
            "Ist die Fehlerquote UEBER der Schwelle, wird die ganze Datei "
            "abgelehnt (Log-Status 'abgelehnt'); DARUNTER werden die guten "
            "Zeilen geladen und die kaputten in die Quarantaene verschoben."
        ),
    )


# --- Gesamt-Konfiguration -----------------------------------------------------


class QuellenConfig(StrictModel):
    """Eine vollstaendige, validierte Konfiguration fuer eine externe
    Datenquelle. Entspricht genau einer YAML-Datei in `config_catalog/`."""

    name: str = Field(..., min_length=1)
    beschreibung: str = ""

    datei: DateiConfig = Field(default_factory=DateiConfig)
    spalten: list[SpaltenDef]
    klassifikation: KlassifikationConfig = Field(default_factory=KlassifikationConfig)
    mapping: MappingConfig
    zielsystem: ZielsystemConfig

    @property
    def spalten_anzahl(self) -> int:
        return len(self.spalten)

    @field_validator("spalten")
    @classmethod
    def spalten_pruefen(cls, v: list[SpaltenDef]) -> list[SpaltenDef]:
        if not v:
            raise ValueError("spalten darf nicht leer sein.")

        positionen = [s.position for s in v]
        if sorted(positionen) != list(range(len(v))):
            raise ValueError(
                f"Spalten-Positionen muessen lueckenlos 0..{len(v) - 1} sein, "
                f"gefunden: {sorted(positionen)}. (Jede Spalte der Datei braucht "
                f"eine Definition, sonst stimmt die Spaltenanzahl-Pruefung nicht.)"
            )

        namen = [s.name for s in v]
        doppelte = {n for n in namen if namen.count(n) > 1}
        if doppelte:
            raise ValueError(f"Doppelte logische Spaltennamen: {sorted(doppelte)}")

        return v

    @model_validator(mode="after")
    def mapping_referenzen_pruefen(self) -> "QuellenConfig":
        """Jede Mapping-Regel muss auf einen existierenden logischen
        Spaltennamen zeigen — Tippfehler hier wuerden sonst erst zur
        Laufzeit beim Mappen einer echten Datei auffallen."""
        bekannte_namen = {s.name for s in self.spalten}
        # Alte Struktur pruefen
        for regel in self.mapping.regeln:
            if regel.quelle not in bekannte_namen:
                raise ValueError(
                    f"mapping.regeln: Quelle '{regel.quelle}' ist nicht im "
                    f"spalten-Block definiert. Bekannte Namen: {sorted(bekannte_namen)}"
                )
        # Neue Struktur (Feld-Kategorien) pruefen
        ziel_namen: set[str] = set()
        for feld in self.mapping.felder:
            if feld.ziel in ziel_namen:
                raise ValueError(
                    f"mapping.felder: Zielname '{feld.ziel}' doppelt vergeben."
                )
            ziel_namen.add(feld.ziel)

            if feld.kategorie == "quelle":
                if not feld.von:
                    raise ValueError(
                        f"mapping.felder[{feld.ziel}]: kategorie=quelle "
                        "braucht das Feld 'von'."
                    )
                if feld.von not in bekannte_namen:
                    raise ValueError(
                        f"mapping.felder[{feld.ziel}]: Quelle '{feld.von}' "
                        f"ist nicht im spalten-Block definiert. "
                        f"Bekannte Namen: {sorted(bekannte_namen)}"
                    )
            elif feld.kategorie == "system":
                if not feld.system_quelle:
                    raise ValueError(
                        f"mapping.felder[{feld.ziel}]: kategorie=system "
                        "braucht das Feld 'system_quelle'."
                    )
                if feld.system_quelle == "pfad_regex" and not feld.regex:
                    raise ValueError(
                        f"mapping.felder[{feld.ziel}]: system_quelle=pfad_regex "
                        "braucht zusaetzlich das Feld 'regex'."
                    )
                if feld.system_quelle == "pfad_regex" and feld.regex:
                    # Regex-Kompilierbarkeit gleich hier pruefen — sonst
                    # taucht der Fehler erst beim Verarbeiten der ersten Zeile auf.
                    import re as _re
                    try:
                        _re.compile(feld.regex)
                    except _re.error as e:
                        raise ValueError(
                            f"mapping.felder[{feld.ziel}]: regex ist ungueltig: {e}"
                        )
            elif feld.kategorie == "berechnet":
                if not feld.formel:
                    raise ValueError(
                        f"mapping.felder[{feld.ziel}]: kategorie=berechnet "
                        "braucht das Feld 'formel'."
                    )
            # 'auto' braucht keine weiteren Felder — DB setzt den Wert
        return self