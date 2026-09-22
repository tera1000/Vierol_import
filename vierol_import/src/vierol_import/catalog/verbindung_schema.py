"""
Meta-Schema fuer Zielsystem-Verbindungen.

Verbindungen liegen in eigenen YAML-Dateien im Ordner `zielsysteme/`.
Eine Quellen-Config verweist per `zielsystem.ref` auf eine solche
Verbindung. So sind Verbindungsdetails und die eigentliche Quellen-
Definition sauber getrennt.

Struktur pro Verbindung:

    zielsysteme/vierol_oracle_prod.yaml
    ---
    name: vierol_oracle_prod
    beschreibung: "Vierol Oracle Produktiv"
    typ: oracle
    user: wilson.tematio
    dsn: dbserver.vierol.local:1521/PROD
    schema: DWHRR

    zielsysteme/test_lokal.yaml
    ---
    name: test_lokal
    beschreibung: "Lokale SQLite fuer Tests"
    typ: sqlite
    pfad: data/vierol_import.sqlite

WICHTIG — Passwoerter: Sie stehen NICHT in der YAML. Fuer Oracle-
Verbindungen wird das Passwort zur Laufzeit interaktiv erfragt (in der
GUI per Dialog, in der CLI per getpass.getpass) und ausschliesslich im
Arbeitsspeicher gehalten. Nach dem Ende der Session ist es weg.

Dieses Sicherheitsmodell verhindert, dass Passwoerter versehentlich in
Backups, Git-Commits oder Log-Dateien landen.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Wie beim Quellen-Meta-Schema: unbekannte Felder ablehnen, damit
    Tippfehler beim Anlegen einer Verbindung sofort auffallen."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Verbindung(StrictModel):
    """Verbindung zu einem Zielsystem (SQLite lokal oder Oracle)."""

    name: str = Field(
        description="Eindeutiger Name; muss mit dem Dateinamen uebereinstimmen."
    )
    beschreibung: str = Field(
        default="",
        description="Klartext-Beschreibung fuer die GUI-Anzeige (optional).",
    )
    typ: Literal["sqlite", "oracle"] = Field(
        description="Art des Zielsystems. Bestimmt, welcher Loader benutzt wird."
    )

    # --- SQLite-spezifisch ---
    pfad: str | None = Field(
        default=None,
        description=(
            "Nur bei typ=sqlite: Pfad zur .sqlite-Datei "
            "(relativ zum Projekt-Root)."
        ),
    )

    # --- Oracle-spezifisch ---
    # Passwort steht NICHT in der YAML — es wird zur Laufzeit interaktiv
    # abgefragt (GUI-Dialog bzw. CLI-Prompt) und nur im RAM gehalten.
    # Nach dem Programmende ist es weg. Das ist der sicherste Weg.
    user: str | None = Field(
        default=None,
        description="Nur bei typ=oracle: Datenbank-Benutzername.",
    )
    dsn: str | None = Field(
        default=None,
        description=(
            "Nur bei typ=oracle: Verbindungs-String im Format "
            "host:port/service (z.B. dbserver.vierol.local:1521/PROD)."
        ),
    )
    schema_: str | None = Field(
        default=None, alias="schema",
        description="Nur bei typ=oracle: optionaler Schema-Praefix (z.B. DWHRR).",
    )