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
                  plus Wertebereiche (minimum/maximum, pflicht).
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
      - {position: 2, name: bezeichnung,   typ: string}
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
    sind pro Quelle stabil und dienen als K.O.-Kriterium der Erkennung."""

    trennzeichen: str = ";"
    encoding: str = "utf-8"
    hat_header: bool = False


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


class MappingConfig(StrictModel):
    regeln: list[MappingRegel]
    abgeleitete_felder: list[AbgeleitetesFeld] = Field(default_factory=list)

    @field_validator("regeln")
    @classmethod
    def mindestens_eine_regel(cls, v: list[MappingRegel]) -> list[MappingRegel]:
        if not v:
            raise ValueError("mapping.regeln darf nicht leer sein.")
        return v


# --- Zielsystem (Load) --------------------------------------------------------


class ZielsystemConfig(StrictModel):
    typ: Literal["sqlite"] = "sqlite"
    tabelle: str
    upsert_key: list[str] = Field(default_factory=list)
    pk_konflikt: Literal["skip", "update", "reject"] = Field(
        default="skip",
        description=(
            "Verhalten bei bereits existierendem Primaerschluessel:\n"
            "  - skip   (Default): neuen Datensatz ueberspringen, alten behalten\n"
            "  - update: neuen Datensatz einspielen, alten ueberschreiben\n"
            "  - reject: ganze Datei ablehnen, wenn auch nur ein Konflikt auftritt"
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
        for regel in self.mapping.regeln:
            if regel.quelle not in bekannte_namen:
                raise ValueError(
                    f"mapping.regeln: Quelle '{regel.quelle}' ist nicht im "
                    f"spalten-Block definiert. Bekannte Namen: {sorted(bekannte_namen)}"
                )
        return self