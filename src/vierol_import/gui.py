"""
Streamlit-GUI fuer den Vierol Import.

Zweck: dieselbe Engine wie das CLI, aber als Browser-Oberflaeche fuer
den Fachbereich, der kein Terminal bedient. Der Aufbau folgt exakt dem
CLI-Workflow (Vorschlag -> Validierung -> Vorschau -> Bestaetigung ->
Load), nur mit Buttons und Tabellen statt Textprompts.

Starten:
    streamlit run src/vierol_import/gui.py

Wichtige Design-Entscheidung zum Datei-Verschieben:
Die GUI ist ein interaktives Testwerkzeug. Wir verschieben NUR, wenn
etwas Auditrelevantes passiert ist:

  * Erfolgreicher Schreibvorgang -> archive/ (fuer die Historie, wer
    hat wann was geladen)
  * Ablehnung, die der User explizit weiterreichen will -> reject/
    (per Button, nur wenn er den Fehler festhalten moechte)

Reine Testabbrueche ("Doch nicht" oder Abbrechen nach Fehler) werden
weder archiviert noch rejected — die temporaere Datei wird geloescht
und es gibt nur einen Log-Eintrag. So kann der Fachbereich beliebig
oft probieren, ohne Datei-Muell zu erzeugen.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere
from vierol_import.engine import ImportEngine, Status
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
    """Katalog laden und Engine aufbauen. Caching: pro Session einmal."""
    result = load_catalog(DEFAULT_CATALOG)
    if not result.configs:
        st.error("Keine gueltigen Configs im Katalog gefunden.")
        st.stop()
    engine = ImportEngine(result.configs, db_pfad=DEFAULT_DB)
    return engine, sorted(result.configs)


# --- Session-State ----------------------------------------------------------


if "temp_pfad" not in st.session_state:
    st.session_state.temp_pfad = None
if "ergebnis" not in st.session_state:
    st.session_state.ergebnis = None
if "gewaehlte_quelle" not in st.session_state:
    st.session_state.gewaehlte_quelle = None


def _reset(grund: str = "") -> None:
    """Session zuruecksetzen, Temp-Datei loeschen. Optional mit Log-Grund."""
    if st.session_state.temp_pfad and st.session_state.temp_pfad.exists():
        datei_name = st.session_state.temp_pfad.name
        st.session_state.temp_pfad.unlink(missing_ok=True)
        if grund:
            logger.info("GUI: Test '%s' beendet ohne Verschieben — %s",
                        datei_name, grund)
    st.session_state.temp_pfad = None
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

if upload is not None and st.session_state.temp_pfad is None:
    tmp = NamedTemporaryFile(
        delete=False, suffix=Path(upload.name).suffix, prefix="upload_"
    )
    tmp.write(upload.getvalue())
    tmp.close()
    # Auf den Originalnamen umbenennen, damit Archiv/Reject-Namen sinnvoll sind
    ziel = Path(tmp.name).parent / upload.name
    Path(tmp.name).rename(ziel)
    st.session_state.temp_pfad = ziel
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None
    logger.info("GUI: Datei '%s' hochgeladen", upload.name)


if st.session_state.temp_pfad is None:
    st.info("Bitte eine Datei zum Import auswaehlen.")
    st.stop()

datei = st.session_state.temp_pfad
st.success(f"Datei bereit: **{datei.name}** ({datei.stat().st_size:,} Byte)")

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


# Schritt 3: Pruefen
st.header("3. Pruefen")

col_pruefen, col_reset = st.columns([1, 1])
with col_pruefen:
    pruefen = st.button("🔍 Pruefen", type="primary", use_container_width=True)
with col_reset:
    if st.button("❌ Abbrechen", use_container_width=True):
        _reset(grund="User-Abbruch vor Pruefung")
        st.rerun()

if pruefen:
    st.session_state.ergebnis = engine.verarbeite_mit_quelle(
        datei, st.session_state.gewaehlte_quelle
    )

ergebnis = st.session_state.ergebnis
if ergebnis is None:
    st.stop()

# --- Fall A: Pruefung fehlgeschlagen -----------------------------------------

if not ergebnis.bereit_zum_schreiben:
    st.error(f"**Abgelehnt:** {ergebnis.fehler_grund}")
    if ergebnis.fehler_details:
        with st.expander(
            f"Fehlerdetails ({len(ergebnis.fehler_details)} Eintraege)"
        ):
            for f in ergebnis.fehler_details[:100]:
                st.text(f)

    st.write(
        "Was moechten Sie mit dieser Datei tun?"
    )
    col_reject, col_verwerfen = st.columns([1, 1])
    with col_reject:
        if st.button(
            "📁 Datei in Reject verschieben (protokollieren)",
            use_container_width=True,
        ):
            ziel = verschiebe_ins_reject(
                datei, DEFAULT_REJECT,
                ergebnis.fehler_grund, ergebnis.fehler_details,
            )
            st.success(f"Verschoben nach: `{ziel}`")
            logger.info("GUI: '%s' in Reject verschoben", datei.name)
            _reset()
            st.rerun()
    with col_verwerfen:
        if st.button(
            "🗑️ Test verwerfen (nichts speichern)",
            use_container_width=True,
        ):
            _reset(grund=f"User verwarf fehlgeschlagenen Test — {ergebnis.fehler_grund}")
            st.rerun()
    st.stop()

# --- Fall B: Pruefung erfolgreich, Vorschau ---------------------------------

st.header("4. Vorschau")
assert ergebnis.mapping is not None and ergebnis.cfg is not None

st.info(
    "🔒 **Test-Modus:** Bisher wurde noch **nichts** in die Datenbank geschrieben. "
    "Die Vorschau unten zeigt, wie die Daten aussehen **wuerden**, wenn Sie unten "
    "auf **'In Zieltabelle schreiben'** klicken."
)

st.write(
    f"→ **{len(ergebnis.mapping.saetze)}** Datensaetze fuer Zieltabelle "
    f"**`{ergebnis.cfg.zielsystem.tabelle}`** (PK-Konflikt-Modus: `{ergebnis.cfg.zielsystem.pk_konflikt}`)."
)

df = pd.DataFrame(ergebnis.mapping.saetze, columns=ergebnis.mapping.zielfelder)
st.dataframe(df, hide_index=True, use_container_width=True)

# Schritt 5: Bestaetigung + Schreiben
st.header("5. Schreiben")

col_ok, col_cancel = st.columns([1, 1])
with col_ok:
    schreiben = st.button(
        "✅ In Zieltabelle schreiben", type="primary", use_container_width=True
    )
with col_cancel:
    if st.button("⏸️ Doch nicht", use_container_width=True):
        _reset(grund="User verwarf erfolgreichen Test nach Vorschau")
        st.rerun()

if schreiben:
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
        logger.info("GUI: '%s' erfolgreich geladen und archiviert", datei.name)
    else:
        # PK-Konflikt (reject-Modus) oder anderer Load-Fehler:
        # analog zur Pruefungsablehnung -> Reject anbieten
        st.error(f"❌ **Fehler beim Schreiben:** {ergebnis.fehler_grund}")
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.info(f"Datei verschoben nach: `{ziel}`")
        logger.info("GUI: '%s' beim Schreiben abgelehnt, in Reject",
                    datei.name)

    _reset()
    if st.button("Weitere Datei importieren"):
        st.rerun()