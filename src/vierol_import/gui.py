"""
Streamlit-GUI fuer den Vierol Import.

Konzept: Sandbox-Testen vs. Ernstfall.

Der Fachbereich soll eine Datei beliebig oft pruefen koennen — mit
sofortigem Feedback, ohne Nebenwirkungen. Erst wenn er explizit auf
"Schreiben" klickt, wird der Vorgang scharf gemacht (SQLite-Insert,
Archivierung/Reject, Log-Eintrag).

Konsequenz fuer die Implementierung:
  - "Pruefen"     -> nur Speicher-Operationen, keine Log-Eintraege,
                     kein Datei-Verschieben. Fehler werden im UI
                     angezeigt, damit der User die Datei oder die
                     Config anpassen kann.
  - "Schreiben"   -> lade_sqlite() + verschiebe_ins_archiv() +
                     Log-Eintrag (Audit-Ereignis).
  - Beim Ausstieg -> Temp-Datei aufraeumen, kein Log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere
from vierol_import.engine import ImportEngine
from vierol_import.ingest.watcher import verschiebe_ins_archiv, verschiebe_ins_reject
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_DB = Path("data/vierol_import.sqlite")
DEFAULT_ARCHIVE = Path("data/archive")
DEFAULT_REJECT = Path("data/reject")

setup_logging(verbose=False)
logger = logging.getLogger("vierol_import.gui")

st.set_page_config(page_title="Vierol Import", page_icon="📥", layout="wide")


# --- Katalog laden (cached) -------------------------------------------------


@st.cache_resource
def get_engine() -> tuple[ImportEngine, list[str]]:
    result = load_catalog(DEFAULT_CATALOG)
    if not result.configs:
        st.error("Keine gueltigen Configs im Katalog gefunden.")
        st.stop()
    engine = ImportEngine(result.configs, db_pfad=DEFAULT_DB)
    return engine, sorted(result.configs)


# --- Session-State ----------------------------------------------------------


if "temp_pfad" not in st.session_state:
    st.session_state.temp_pfad = None
if "letzter_upload_name" not in st.session_state:
    st.session_state.letzter_upload_name = None
if "ergebnis" not in st.session_state:
    st.session_state.ergebnis = None
if "gewaehlte_quelle" not in st.session_state:
    st.session_state.gewaehlte_quelle = None


def _temp_datei_loeschen() -> None:
    """Aufraeumen der Sandbox-Datei — laesst keine Spuren im Temp-Ordner."""
    if st.session_state.temp_pfad and st.session_state.temp_pfad.exists():
        st.session_state.temp_pfad.unlink(missing_ok=True)


def _reset() -> None:
    """Session komplett zuruecksetzen."""
    _temp_datei_loeschen()
    st.session_state.temp_pfad = None
    st.session_state.letzter_upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None


# --- Layout -----------------------------------------------------------------


st.title("📥 Vierol Import")
st.caption("Metadaten-gesteuerte Import-Pipeline — GUI fuer den Fachbereich")

engine, quellen = get_engine()

# Schritt 1: Datei-Upload
st.header("1. Datei hochladen")
upload = st.file_uploader(
    "Datei auswaehlen oder hierher ziehen",
    type=None,
    accept_multiple_files=False,
)

# Neuer Upload erkannt: alte Temp-Datei aufraeumen, neue anlegen.
# Der Vergleich mit `letzter_upload_name` verhindert, dass jeder Streamlit-
# Rerun die Datei erneut anlegt (Streamlit fuehrt das Skript bei jeder
# Interaktion vollstaendig aus).
if upload is not None and upload.name != st.session_state.letzter_upload_name:
    _temp_datei_loeschen()

    tmp = NamedTemporaryFile(
        delete=False, suffix=Path(upload.name).suffix, prefix="vierol_upload_"
    )
    tmp.write(upload.getvalue())
    tmp.close()

    ziel = Path(tmp.name).parent / f"vierol_{upload.name}"
    os.replace(tmp.name, ziel)

    st.session_state.temp_pfad = ziel
    st.session_state.letzter_upload_name = upload.name
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None


if st.session_state.temp_pfad is None:
    st.info("Bitte eine Datei zum Import auswaehlen.")
    st.stop()

datei = st.session_state.temp_pfad
st.success(
    f"Datei bereit: **{st.session_state.letzter_upload_name}** "
    f"({datei.stat().st_size:,} Byte)"
)
st.caption(
    "💡 Sie koennen diese Datei beliebig oft pruefen. "
    "Erst mit 'In Zieltabelle schreiben' wird sie in die Datenbank uebernommen."
)


# Schritt 2: Erkennung
st.header("2. Quelle waehlen")

ranking = klassifiziere(datei, engine.configs)

ranking_daten = []
for e in ranking.ergebnisse:
    ranking_daten.append(
        {
            "Quelle": e.quelle,
            "Score": f"{e.score:.0%}" if e.moeglich else "—",
            "Status": "kandidat" if e.moeglich else f"K.O.: {e.ko_grund}",
        }
    )
st.dataframe(pd.DataFrame(ranking_daten), hide_index=True, use_container_width=True)

kandidaten = [e for e in ranking.ergebnisse if e.moeglich]
default_index = 0
if kandidaten:
    bester = kandidaten[0]
    schwelle = engine.configs[bester.quelle].klassifikation.schwellenwert
    if bester.score >= schwelle:
        default_index = quellen.index(bester.quelle)
        st.info(f"Empfehlung: **{bester.quelle}** ({bester.score:.0%})")
    else:
        st.warning(
            f"Kein sicherer Vorschlag (bester Score {bester.score:.0%} liegt "
            f"unter Schwellenwert {schwelle:.0%}). Bitte manuell waehlen."
        )
else:
    st.warning("Keine Quelle passt strukturell zu dieser Datei.")

st.session_state.gewaehlte_quelle = st.selectbox(
    "Quelle:", options=quellen, index=default_index
)


# Schritt 3: Pruefen (Sandbox-Modus — beliebig oft moeglich)
st.header("3. Pruefen")

col_pruefen, col_neu = st.columns([1, 1])
with col_pruefen:
    pruefen = st.button(
        "🔍 Pruefen (ohne Speichern)", type="primary", use_container_width=True
    )
with col_neu:
    if st.button("🔄 Andere Datei", use_container_width=True):
        _reset()
        st.rerun()

if pruefen:
    # Bewusst keine Log-Zeile — reines Sandbox-Testen.
    st.session_state.ergebnis = engine.verarbeite_mit_quelle(
        datei, st.session_state.gewaehlte_quelle
    )

ergebnis = st.session_state.ergebnis
if ergebnis is None:
    st.stop()

# --- Fall A: Pruefung fehlgeschlagen ----------------------------------------

if not ergebnis.bereit_zum_schreiben:
    st.error(f"**Nicht bereit zum Schreiben:** {ergebnis.fehler_grund}")
    if ergebnis.fehler_details:
        with st.expander(
            f"Fehlerdetails ({len(ergebnis.fehler_details)} Eintraege)"
        ):
            for f in ergebnis.fehler_details[:100]:
                st.text(f)
    st.info(
        "💡 Naechste Schritte: Passen Sie die Datei oder die Konfiguration an, "
        "und pruefen Sie erneut. Nichts wurde in die Datenbank geschrieben."
    )
    st.stop()

# --- Fall B: Pruefung erfolgreich, Vorschau ---------------------------------

st.header("4. Vorschau")
assert ergebnis.mapping is not None and ergebnis.cfg is not None

st.success(
    "✓ Datei ist gueltig und bereit. "
    "**Noch nichts in die Datenbank geschrieben** — die Vorschau zeigt, "
    "wie die Daten aussehen wuerden."
)

st.write(
    f"→ **{len(ergebnis.mapping.saetze)}** Datensaetze "
    f"fuer Zieltabelle **`{ergebnis.cfg.zielsystem.tabelle}`** "
    f"(PK-Konflikt-Modus: `{ergebnis.cfg.zielsystem.pk_konflikt}`)."
)

df = pd.DataFrame(ergebnis.mapping.saetze, columns=ergebnis.mapping.zielfelder)
st.dataframe(df, hide_index=True, use_container_width=True)


# Schritt 5: Schreiben (Ernstfall — hier beginnt die Protokollierung)
st.header("5. In die Datenbank schreiben")

st.warning(
    "⚠️ **Ab hier wird es ernst:** Die Datensaetze werden in die Zieltabelle "
    "eingefuegt und die Datei wird ins Archiv verschoben. Der Vorgang wird "
    "protokolliert."
)

schreiben = st.button(
    "✅ In Zieltabelle schreiben", type="primary", use_container_width=True
)

if schreiben:
    logger.info(
        "GUI: Schreibvorgang gestartet — Datei '%s', Quelle '%s'",
        st.session_state.letzter_upload_name, ergebnis.quelle,
    )
    engine.schreibe(ergebnis)

    if ergebnis.erfolg:
        ziel = verschiebe_ins_archiv(datei, DEFAULT_ARCHIVE, ergebnis.quelle)  # type: ignore[arg-type]
        st.success(
            f"✅ **Erfolgreich:** {ergebnis.zeilen_geladen} Datensaetze geladen"
            + (
                f", {ergebnis.zeilen_uebersprungen} uebersprungen (PK existierte)"
                if ergebnis.zeilen_uebersprungen
                else ""
            )
            + f".\n\nDatei archiviert: `{ziel}`"
        )
        logger.info(
            "GUI: '%s' erfolgreich geladen (%d geladen, %d uebersprungen)",
            st.session_state.letzter_upload_name,
            ergebnis.zeilen_geladen, ergebnis.zeilen_uebersprungen,
        )
    else:
        # Fehler erst beim Schreiben (z. B. PK-Konflikt im reject-Modus).
        # -> Datei ins Reject verschieben, weil der Ernstfall-Versuch fehlgeschlagen ist.
        st.error(f"❌ **Fehler beim Schreiben:** {ergebnis.fehler_grund}")
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.info(f"Datei verschoben nach: `{ziel}`")
        logger.info(
            "GUI: '%s' beim Schreiben abgelehnt (%s), in Reject verschoben",
            st.session_state.letzter_upload_name, ergebnis.fehler_grund,
        )

    # Session zuruecksetzen fuer den naechsten Vorgang.
    st.session_state.temp_pfad = None  # schon verschoben, nicht mehr loeschen
    st.session_state.letzter_upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None
    """
Streamlit-GUI fuer den Vierol Import.

Konzept: Sandbox-Testen vs. Ernstfall.

Der Fachbereich soll eine Datei beliebig oft pruefen koennen — mit
sofortigem Feedback, ohne Nebenwirkungen. Erst wenn er explizit auf
"Schreiben" klickt, wird der Vorgang scharf gemacht (SQLite-Insert,
Archivierung/Reject, Log-Eintrag).

Konsequenz fuer die Implementierung:
  - "Pruefen"     -> nur Speicher-Operationen, keine Log-Eintraege,
                     kein Datei-Verschieben. Fehler werden im UI
                     angezeigt, damit der User die Datei oder die
                     Config anpassen kann.
  - "Schreiben"   -> lade_sqlite() + verschiebe_ins_archiv() +
                     Log-Eintrag (Audit-Ereignis).
  - Beim Ausstieg -> Temp-Datei aufraeumen, kein Log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere
from vierol_import.engine import ImportEngine
from vierol_import.ingest.watcher import verschiebe_ins_archiv, verschiebe_ins_reject
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_DB = Path("data/vierol_import.sqlite")
DEFAULT_ARCHIVE = Path("data/archive")
DEFAULT_REJECT = Path("data/reject")

setup_logging(verbose=False)
logger = logging.getLogger("vierol_import.gui")

st.set_page_config(page_title="Vierol Import", page_icon="📥", layout="wide")


# --- Katalog laden (cached) -------------------------------------------------


@st.cache_resource
def get_engine() -> tuple[ImportEngine, list[str]]:
    result = load_catalog(DEFAULT_CATALOG)
    if not result.configs:
        st.error("Keine gueltigen Configs im Katalog gefunden.")
        st.stop()
    engine = ImportEngine(result.configs, db_pfad=DEFAULT_DB)
    return engine, sorted(result.configs)


# --- Session-State ----------------------------------------------------------


if "temp_pfad" not in st.session_state:
    st.session_state.temp_pfad = None
if "letzter_upload_name" not in st.session_state:
    st.session_state.letzter_upload_name = None
if "ergebnis" not in st.session_state:
    st.session_state.ergebnis = None
if "gewaehlte_quelle" not in st.session_state:
    st.session_state.gewaehlte_quelle = None


def _temp_datei_loeschen() -> None:
    """Aufraeumen der Sandbox-Datei — laesst keine Spuren im Temp-Ordner."""
    if st.session_state.temp_pfad and st.session_state.temp_pfad.exists():
        st.session_state.temp_pfad.unlink(missing_ok=True)


def _reset() -> None:
    """Session komplett zuruecksetzen."""
    _temp_datei_loeschen()
    st.session_state.temp_pfad = None
    st.session_state.letzter_upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None


# --- Layout -----------------------------------------------------------------


st.title("📥 Vierol Import")
st.caption("Metadaten-gesteuerte Import-Pipeline — GUI fuer den Fachbereich")

engine, quellen = get_engine()

# Schritt 1: Datei-Upload
st.header("1. Datei hochladen")
upload = st.file_uploader(
    "Datei auswaehlen oder hierher ziehen",
    type=None,
    accept_multiple_files=False,
)

# Neuer Upload erkannt: alte Temp-Datei aufraeumen, neue anlegen.
# Der Vergleich mit `letzter_upload_name` verhindert, dass jeder Streamlit-
# Rerun die Datei erneut anlegt (Streamlit fuehrt das Skript bei jeder
# Interaktion vollstaendig aus).
if upload is not None and upload.name != st.session_state.letzter_upload_name:
    _temp_datei_loeschen()

    tmp = NamedTemporaryFile(
        delete=False, suffix=Path(upload.name).suffix, prefix="vierol_upload_"
    )
    tmp.write(upload.getvalue())
    tmp.close()

    ziel = Path(tmp.name).parent / f"vierol_{upload.name}"
    os.replace(tmp.name, ziel)

    st.session_state.temp_pfad = ziel
    st.session_state.letzter_upload_name = upload.name
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None


if st.session_state.temp_pfad is None:
    st.info("Bitte eine Datei zum Import auswaehlen.")
    st.stop()

datei = st.session_state.temp_pfad
st.success(
    f"Datei bereit: **{st.session_state.letzter_upload_name}** "
    f"({datei.stat().st_size:,} Byte)"
)
st.caption(
    "💡 Sie koennen diese Datei beliebig oft pruefen. "
    "Erst mit 'In Zieltabelle schreiben' wird sie in die Datenbank uebernommen."
)


# Schritt 2: Erkennung
st.header("2. Quelle waehlen")

ranking = klassifiziere(datei, engine.configs)

ranking_daten = []
for e in ranking.ergebnisse:
    ranking_daten.append(
        {
            "Quelle": e.quelle,
            "Score": f"{e.score:.0%}" if e.moeglich else "—",
            "Status": "kandidat" if e.moeglich else f"K.O.: {e.ko_grund}",
        }
    )
st.dataframe(pd.DataFrame(ranking_daten), hide_index=True, use_container_width=True)

kandidaten = [e for e in ranking.ergebnisse if e.moeglich]
default_index = 0
if kandidaten:
    bester = kandidaten[0]
    schwelle = engine.configs[bester.quelle].klassifikation.schwellenwert
    if bester.score >= schwelle:
        default_index = quellen.index(bester.quelle)
        st.info(f"Empfehlung: **{bester.quelle}** ({bester.score:.0%})")
    else:
        st.warning(
            f"Kein sicherer Vorschlag (bester Score {bester.score:.0%} liegt "
            f"unter Schwellenwert {schwelle:.0%}). Bitte manuell waehlen."
        )
else:
    st.warning("Keine Quelle passt strukturell zu dieser Datei.")

st.session_state.gewaehlte_quelle = st.selectbox(
    "Quelle:", options=quellen, index=default_index
)


# Schritt 3: Pruefen (Sandbox-Modus — beliebig oft moeglich)
st.header("3. Pruefen")

col_pruefen, col_neu = st.columns([1, 1])
with col_pruefen:
    pruefen = st.button(
        "🔍 Pruefen (ohne Speichern)", type="primary", use_container_width=True
    )
with col_neu:
    if st.button("🔄 Andere Datei", use_container_width=True):
        _reset()
        st.rerun()

if pruefen:
    # Bewusst keine Log-Zeile — reines Sandbox-Testen.
    st.session_state.ergebnis = engine.verarbeite_mit_quelle(
        datei, st.session_state.gewaehlte_quelle
    )

ergebnis = st.session_state.ergebnis
if ergebnis is None:
    st.stop()

# --- Fall A: Pruefung fehlgeschlagen ----------------------------------------

if not ergebnis.bereit_zum_schreiben:
    st.error(f"**Nicht bereit zum Schreiben:** {ergebnis.fehler_grund}")
    if ergebnis.fehler_details:
        with st.expander(
            f"Fehlerdetails ({len(ergebnis.fehler_details)} Eintraege)"
        ):
            for f in ergebnis.fehler_details[:100]:
                st.text(f)
    st.info(
        "💡 Naechste Schritte: Passen Sie die Datei oder die Konfiguration an, "
        "und pruefen Sie erneut. Nichts wurde in die Datenbank geschrieben."
    )
    st.stop()

# --- Fall B: Pruefung erfolgreich, Vorschau ---------------------------------

st.header("4. Vorschau")
assert ergebnis.mapping is not None and ergebnis.cfg is not None

st.success(
    "✓ Datei ist gueltig und bereit. "
    "**Noch nichts in die Datenbank geschrieben** — die Vorschau zeigt, "
    "wie die Daten aussehen wuerden."
)

st.write(
    f"→ **{len(ergebnis.mapping.saetze)}** Datensaetze "
    f"fuer Zieltabelle **`{ergebnis.cfg.zielsystem.tabelle}`** "
    f"(PK-Konflikt-Modus: `{ergebnis.cfg.zielsystem.pk_konflikt}`)."
)

df = pd.DataFrame(ergebnis.mapping.saetze, columns=ergebnis.mapping.zielfelder)
st.dataframe(df, hide_index=True, use_container_width=True)


# Schritt 5: Schreiben (Ernstfall — hier beginnt die Protokollierung)
st.header("5. In die Datenbank schreiben")

st.warning(
    "⚠️ **Ab hier wird es ernst:** Die Datensaetze werden in die Zieltabelle "
    "eingefuegt und die Datei wird ins Archiv verschoben. Der Vorgang wird "
    "protokolliert."
)

schreiben = st.button(
    "✅ In Zieltabelle schreiben", type="primary", use_container_width=True
)

if schreiben:
    logger.info(
        "GUI: Schreibvorgang gestartet — Datei '%s', Quelle '%s'",
        st.session_state.letzter_upload_name, ergebnis.quelle,
    )
    engine.schreibe(ergebnis)

    if ergebnis.erfolg:
        ziel = verschiebe_ins_archiv(datei, DEFAULT_ARCHIVE, ergebnis.quelle)  # type: ignore[arg-type]
        st.success(
            f"✅ **Erfolgreich:** {ergebnis.zeilen_geladen} Datensaetze geladen"
            + (
                f", {ergebnis.zeilen_uebersprungen} uebersprungen (PK existierte)"
                if ergebnis.zeilen_uebersprungen
                else ""
            )
            + f".\n\nDatei archiviert: `{ziel}`"
        )
        logger.info(
            "GUI: '%s' erfolgreich geladen (%d geladen, %d uebersprungen)",
            st.session_state.letzter_upload_name,
            ergebnis.zeilen_geladen, ergebnis.zeilen_uebersprungen,
        )
    else:
        # Fehler erst beim Schreiben (z. B. PK-Konflikt im reject-Modus).
        # -> Datei ins Reject verschieben, weil der Ernstfall-Versuch fehlgeschlagen ist.
        st.error(f"❌ **Fehler beim Schreiben:** {ergebnis.fehler_grund}")
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.info(f"Datei verschoben nach: `{ziel}`")
        logger.info(
            "GUI: '%s' beim Schreiben abgelehnt (%s), in Reject verschoben",
            st.session_state.letzter_upload_name, ergebnis.fehler_grund,
        )

    # Session zuruecksetzen fuer den naechsten Vorgang.
    st.session_state.temp_pfad = None  # schon verschoben, nicht mehr loeschen
    st.session_state.letzter_upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None