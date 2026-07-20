"""
Streamlit-GUI fuer den Vierol Import.

Zweck: dieselbe Engine wie das CLI, aber als Browser-Oberflaeche fuer
den Fachbereich, der kein Terminal bedient. Der Aufbau folgt exakt dem
CLI-Workflow (Vorschlag -> Validierung -> Vorschau -> Bestaetigung ->
Load), nur mit Buttons und Tabellen statt Textprompts.

Starten:
    streamlit run src/vierol_import/gui.py

Die App ist bewusst zustandsbehaftet (st.session_state), damit
mehrstufige Interaktionen (Datei -> Quelle waehlen -> pruefen ->
schreiben) sauber ablaufen, ohne dass die Seite jeden Zwischenschritt
verliert.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere
from vierol_import.engine import ImportEngine, Status
from vierol_import.ingest.watcher import verschiebe_ins_archiv, verschiebe_ins_reject

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_DB = Path("data/vierol_import.sqlite")
DEFAULT_ARCHIVE = Path("data/archive")
DEFAULT_REJECT = Path("data/reject")

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
# Wir merken uns Upload und Verarbeitungs-Zustand ueber Reruns hinweg.


if "temp_pfad" not in st.session_state:
    st.session_state.temp_pfad = None
if "ergebnis" not in st.session_state:
    st.session_state.ergebnis = None
if "gewaehlte_quelle" not in st.session_state:
    st.session_state.gewaehlte_quelle = None


def _reset() -> None:
    """Session zuruecksetzen (nach Fertigstellung oder Fehler)."""
    if st.session_state.temp_pfad and st.session_state.temp_pfad.exists():
        st.session_state.temp_pfad.unlink(missing_ok=True)
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
    type=None,  # alle Dateitypen erlaubt
    accept_multiple_files=False,
)

if upload is not None and st.session_state.temp_pfad is None:
    # Datei zwischenspeichern (Streamlit liefert nur Bytes, kein Pfad).
    # Die Engine braucht aber einen Dateipfad zum Verschieben ins Archiv.
    tmp = NamedTemporaryFile(
        delete=False, suffix=Path(upload.name).suffix, prefix="upload_"
    )
    tmp.write(upload.getvalue())
    tmp.close()
    # Wir benennen die Temp-Datei auf den Original-Namen um, damit sie im
    # Archiv/Reject unter dem echten Namen landet.
    ziel = Path(tmp.name).parent / upload.name
    Path(tmp.name).rename(ziel)
    st.session_state.temp_pfad = ziel
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None


if st.session_state.temp_pfad is None:
    st.info("Bitte eine Datei zum Import auswaehlen.")
    st.stop()

datei = st.session_state.temp_pfad
st.success(f"Datei bereit: **{datei.name}** ({datei.stat().st_size:,} Byte)")

# Schritt 2: Erkennung
st.header("2. Quelle waehlen")

ranking = klassifiziere(datei, engine.configs)

# Ranking als Tabelle
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

# Vorauswahl: beste kandidat, falls es einen sicheren gibt
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
        _reset()
        st.rerun()

if pruefen:
    st.session_state.ergebnis = engine.verarbeite_mit_quelle(
        datei, st.session_state.gewaehlte_quelle
    )

ergebnis = st.session_state.ergebnis
if ergebnis is None:
    st.stop()

# Ergebnis der Pruefung anzeigen
if not ergebnis.bereit_zum_schreiben:
    st.error(f"**Abgelehnt:** {ergebnis.fehler_grund}")
    if ergebnis.fehler_details:
        with st.expander(
            f"Fehlerdetails ({len(ergebnis.fehler_details)} Eintraege)"
        ):
            for f in ergebnis.fehler_details[:100]:
                st.text(f)

    if st.button("Datei in Reject verschieben"):
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.success(f"Verschoben nach: `{ziel}`")
        _reset()
        st.rerun()
    st.stop()

# Schritt 4: Vorschau
st.header("4. Vorschau")
assert ergebnis.mapping is not None and ergebnis.cfg is not None

st.write(
    f"**{len(ergebnis.mapping.saetze)}** Datensaetze wuerden in Tabelle "
    f"**`{ergebnis.cfg.zielsystem.tabelle}`** geschrieben "
    f"(PK-Konflikt-Modus: `{ergebnis.cfg.zielsystem.pk_konflikt}`)."
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
        _reset()
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
    else:
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.error(
            f"❌ Ablehnung beim Schreiben: {ergebnis.fehler_grund}\n\n"
            f"Datei verschoben nach: `{ziel}`"
        )

    _reset()
    if st.button("Weitere Datei importieren"):
        st.rerun()