"""
Streamlit-GUI fuer den Vierol Import.

Konzept: Sandbox vs. Ernstfall.

Pruefen darf der User beliebig oft — dabei wird nichts protokolliert
und nichts verschoben, nur im Speicher gerechnet. Erst der Klick auf
"In Zieltabelle schreiben" macht ernst: SQLite-Insert, Archivierung
oder Reject, Log-Eintrag.

Ein wichtiger Punkt aus dem echten Betrieb: Dateien vom Dienstleister
heissen bei jeder Lieferung GLEICH (z. B. immer `preise.csv`). Damit
ein neuer Upload derselben "gleichnamigen" Datei nicht mit einer
vorherigen Session verwechselt wird, identifizieren wir die Datei
ueber einen Inhalts-Hash — nicht ueber den Namen.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from vierol_import.catalog.reader import load_catalog
from vierol_import.classification.classifier import klassifiziere
from vierol_import.engine import ImportEngine
from vierol_import.ingest.watcher import (
    schreibe_quarantaene,
    verschiebe_ins_archiv,
    verschiebe_ins_reject,
)
from vierol_import.ingest.zip_entpacker import (
    ZipEntpackfehler,
    entpacke_rekursiv,
)
from vierol_import.monitoring.logger import setup_logging

DEFAULT_CATALOG = Path("config_catalog")
DEFAULT_DB = Path("data/vierol_import.sqlite")
DEFAULT_ARCHIVE = Path("data/archive")
DEFAULT_REJECT = Path("data/reject")
TEMPLATE_PFAD = DEFAULT_CATALOG / "_TEMPLATE.yaml"

setup_logging(verbose=False)
logger = logging.getLogger("vierol_import.gui")

st.set_page_config(page_title="Vierol Import", page_icon="📥", layout="wide")

# Etwas CSS, damit der Datei-Upload-Bereich groesser wird und Drag&Drop
# deutlicher wirkt.
st.markdown(
    """
    <style>
      [data-testid="stFileUploaderDropzone"] {
          min-height: 180px;
          padding: 30px;
          border: 2px dashed #4a9eff;
      }
      [data-testid="stFileUploaderDropzone"] > div > div {
          font-size: 1.05rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- Katalog laden ----------------------------------------------------------
# NICHT cachen: der Katalog aendert sich, sobald jemand eine neue Config
# hinzufuegt. Kosten sind vernachlaessigbar (Millisekunden).


def get_engine() -> tuple[ImportEngine, list[str]]:
    result = load_catalog(DEFAULT_CATALOG)
    if not result.configs:
        st.error("Keine gueltigen Configs im Katalog gefunden.")
        st.stop()
    engine = ImportEngine(result.configs, db_pfad=DEFAULT_DB)
    return engine, sorted(result.configs)


# --- Session-State ----------------------------------------------------------


for key, default in [
    ("temp_pfad", None),
    ("upload_hash", None),
    ("upload_name", None),
    ("ergebnis", None),
    ("gewaehlte_quelle", None),
    ("neue_quelle_dialog", False),
    ("zip_dateien", None),         # Liste EntpackteDatei nach ZIP-Upload
    ("zip_verzeichnis", None),     # Temp-Ordner mit den entpackten Dateien
    ("zip_batch_status", None),    # dict mit Erfolgs/Fehler-Zaehlern
]:
    if key not in st.session_state:
        st.session_state[key] = default


def _temp_datei_loeschen() -> None:
    if st.session_state.temp_pfad and st.session_state.temp_pfad.exists():
        st.session_state.temp_pfad.unlink(missing_ok=True)


def _session_reset() -> None:
    _temp_datei_loeschen()
    _zip_verzeichnis_aufraeumen()
    st.session_state.temp_pfad = None
    st.session_state.upload_hash = None
    st.session_state.upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None
    st.session_state.zip_dateien = None
    st.session_state.zip_verzeichnis = None
    st.session_state.zip_batch_status = None


def _zip_verzeichnis_aufraeumen() -> None:
    """Loescht den entpackten ZIP-Ordner mitsamt allen Dateien."""
    import shutil
    zv = st.session_state.zip_verzeichnis
    if zv and zv.exists():
        shutil.rmtree(zv, ignore_errors=True)


# --- Layout: Titel + Katalog ------------------------------------------------


st.title("📥 Vierol Import")
st.caption("Metadaten-gesteuerte Import-Pipeline — GUI fuer den Fachbereich")

engine, quellen = get_engine()


# --- Dialog: neue Quelle anlegen --------------------------------------------


def _neue_quelle_dialog() -> None:
    """Modal-artiger Bereich zum Anlegen einer neuen Quellen-Config."""
    st.subheader("➕ Neue Quelle anlegen")
    st.caption(
        "Die Konfiguration wird als YAML-Datei in `config_catalog/` gespeichert. "
        "Vorlage unten laesst sich direkt editieren."
    )

    quellen_name = st.text_input(
        "Quellen-Name (klein, ohne Leerzeichen — wird zum Dateinamen)",
        placeholder="z.B. neue_lieferung",
    )

    # Vorlage laden, damit der User nicht bei Null anfangen muss
    vorlage_inhalt = ""
    if TEMPLATE_PFAD.exists():
        vorlage_inhalt = TEMPLATE_PFAD.read_text(encoding="utf-8")

    yaml_inhalt = st.text_area(
        "YAML-Konfiguration",
        value=vorlage_inhalt,
        height=400,
        help="Vorlage bearbeiten. VS Code (mit YAML-Extension) zeigt auch "
             "Autocomplete/Fehler — dort komfortabler zu editieren.",
    )

    col_prev, col_save, col_close = st.columns([1, 1, 1])
    with col_prev:
        if st.button("🔍 Nur pruefen"):
            _pruefe_yaml_inhalt(yaml_inhalt, quellen_name)
    with col_save:
        if st.button("💾 Speichern", type="primary"):
            if _speichere_yaml(yaml_inhalt, quellen_name):
                st.session_state.neue_quelle_dialog = False
                st.rerun()
    with col_close:
        if st.button("Abbrechen"):
            st.session_state.neue_quelle_dialog = False
            st.rerun()


def _pruefe_yaml_inhalt(yaml_text: str, name: str) -> bool:
    """Testweises Parsen: schreibt in eine Temp-Datei, laesst den Katalog-
    Reader pruefen, gibt Fehler direkt aus."""
    import tempfile
    if not name:
        st.error("Bitte einen Quellen-Namen angeben.")
        return False
    tmpdir = Path(tempfile.mkdtemp(prefix="vierol_configtest_"))
    try:
        (tmpdir / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
        result = load_catalog(tmpdir)
        if result.ok:
            st.success(f"✓ YAML ist gueltig. Bereit zum Speichern als `{name}.yaml`.")
            return True
        st.error("YAML enthaelt Fehler:")
        for fehler in result.fehler.get(name, []):
            st.text(f"  • {fehler}")
        return False
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _speichere_yaml(yaml_text: str, name: str) -> bool:
    """Nur speichern, wenn die YAML gueltig ist."""
    if not _pruefe_yaml_inhalt(yaml_text, name):
        return False
    ziel = DEFAULT_CATALOG / f"{name}.yaml"
    if ziel.exists():
        st.error(f"Datei '{ziel.name}' existiert bereits.")
        return False
    ziel.write_text(yaml_text, encoding="utf-8")
    st.success(f"Neue Quelle gespeichert: `{ziel}`")
    logger.info("GUI: Neue Quellen-Config '%s' angelegt", name)
    return True


if st.session_state.neue_quelle_dialog:
    _neue_quelle_dialog()
    st.stop()


# --- Schritt 1: Datei-Upload ------------------------------------------------


st.header("1. Datei hochladen")
upload = st.file_uploader(
    "Datei per Klick auswaehlen oder in dieses Feld ziehen",
    type=None,
    accept_multiple_files=False,
)

if upload is not None:
    inhalt = upload.getvalue()
    neuer_hash = hashlib.sha256(inhalt).hexdigest()

    if neuer_hash != st.session_state.upload_hash:
        _temp_datei_loeschen()
        _zip_verzeichnis_aufraeumen()

        # Datei in Temp-Bereich speichern (Basis fuer Einzel- oder ZIP-Verarbeitung)
        tmp = NamedTemporaryFile(
            delete=False, suffix=Path(upload.name).suffix, prefix="vierol_upload_"
        )
        tmp.write(inhalt)
        tmp.close()

        hash_kurz = neuer_hash[:8]
        ziel = Path(tmp.name).parent / f"vierol_{hash_kurz}_{upload.name}"
        os.replace(tmp.name, ziel)

        # ZIP? -> entpacken und Batch-Modus vorbereiten
        if upload.name.lower().endswith(".zip"):
            entpack_dir = ziel.with_suffix(".entpackt")
            try:
                entpackt = entpacke_rekursiv(ziel, entpack_dir)
                st.session_state.zip_dateien = entpackt
                st.session_state.zip_verzeichnis = entpack_dir
                # Original-ZIP nicht mehr gebraucht
                ziel.unlink(missing_ok=True)
                # Einzeldatei-Zustand aus lassen; wir sind im ZIP-Modus
                st.session_state.temp_pfad = None
            except ZipEntpackfehler as e:
                st.error(f"ZIP konnte nicht entpackt werden: {e}")
                ziel.unlink(missing_ok=True)
                st.stop()
        else:
            # Normale Einzeldatei
            st.session_state.temp_pfad = ziel
            st.session_state.zip_dateien = None

        st.session_state.upload_hash = neuer_hash
        st.session_state.upload_name = upload.name
        st.session_state.ergebnis = None
        st.session_state.zip_batch_status = None


# --- ZIP-Batch-Modus: eigener Ablauf ----------------------------------------
# Wenn ein ZIP hochgeladen wurde, zeigen wir eine Datei-Uebersicht und
# einen Batch-Verarbeitungs-Button. Der Rest der GUI (Einzeldatei-Flow)
# bleibt danach weitgehend intakt und uebernimmt fuer einzelne Dateien.


if st.session_state.zip_dateien is not None:
    st.header("ZIP-Inhalt")
    zd = st.session_state.zip_dateien
    if not zd:
        st.warning("Das ZIP enthaelt keine Dateien.")
        if st.button("🔄 Anderes hochladen"):
            _session_reset()
            st.rerun()
        st.stop()

    st.write(
        f"**{len(zd)}** Datei(en) im ZIP `{st.session_state.upload_name}` "
        f"({sum(e.groesse for e in zd) / (1024 * 1024):.1f} MB gesamt):"
    )
    df_zip = pd.DataFrame([
        {
            "Pfad im ZIP": e.original_pfad,
            "Groesse (KB)": f"{e.groesse / 1024:.1f}",
        }
        for e in zd
    ])
    st.dataframe(df_zip, hide_index=True, use_container_width=True)

    st.caption(
        "💡 Der Batch-Import verarbeitet alle Dateien nacheinander mit "
        "automatischer Quellen-Erkennung (Schwellenwert der jeweiligen Config). "
        "Dateien ohne sicheren Vorschlag werden abgelehnt. "
        "Erfolge werden ins Archiv verschoben, Fehler ins Reject-Verzeichnis."
    )

    col_batch, col_neu = st.columns([1, 1])
    with col_batch:
        batch_start = st.button(
            "🚀 Alle Dateien aus dem ZIP verarbeiten",
            type="primary", use_container_width=True,
        )
    with col_neu:
        if st.button("🔄 Anderes hochladen", use_container_width=True):
            _session_reset()
            st.rerun()

    if batch_start:
        logger.info(
            "GUI-ZIP: Batch-Import gestartet, %d Datei(en) aus '%s'",
            len(zd), st.session_state.upload_name,
        )
        stats = {"geladen": 0, "abgelehnt": 0, "quarantaene_gesamt": 0}
        fortschritt = st.progress(0.0, text="Starte...")
        ergebnisse_pro_datei = []

        for i, ed in enumerate(zd, start=1):
            fortschritt.progress(
                i / len(zd),
                text=f"[{i}/{len(zd)}] {ed.original_pfad}",
            )
            r = engine.verarbeite_auto_und_schreibe(ed.pfad)

            if r.erfolg:
                ziel = verschiebe_ins_archiv(ed.pfad, DEFAULT_ARCHIVE, r.quelle)  # type: ignore[arg-type]
                stats["geladen"] += 1
                if r.quarantaene_zeilen:
                    schreibe_quarantaene(
                        ed.pfad, DEFAULT_REJECT, r.quarantaene_zeilen
                    )
                    stats["quarantaene_gesamt"] += r.zeilen_quarantaene
                ergebnisse_pro_datei.append({
                    "Datei": ed.original_pfad,
                    "Status": "✅ geladen",
                    "Quelle": r.quelle,
                    "geladen": r.zeilen_geladen,
                    "quarantaene": r.zeilen_quarantaene or "-",
                    "Detail": f"→ {ziel.name}",
                })
            else:
                verschiebe_ins_reject(
                    ed.pfad, DEFAULT_REJECT, r.fehler_grund, r.fehler_details
                )
                stats["abgelehnt"] += 1
                ergebnisse_pro_datei.append({
                    "Datei": ed.original_pfad,
                    "Status": "❌ abgelehnt",
                    "Quelle": r.quelle or "—",
                    "geladen": 0,
                    "quarantaene": "-",
                    "Detail": r.fehler_grund,
                })

        fortschritt.empty()
        st.session_state.zip_batch_status = stats

        if stats["abgelehnt"] == 0:
            st.success(
                f"✅ Alle {stats['geladen']} Dateien erfolgreich verarbeitet."
                + (
                    f" ({stats['quarantaene_gesamt']} Zeilen in Quarantaene)"
                    if stats["quarantaene_gesamt"] else ""
                )
            )
        else:
            st.warning(
                f"Verarbeitung fertig: {stats['geladen']} geladen, "
                f"{stats['abgelehnt']} abgelehnt."
            )

        st.subheader("Ergebnisse pro Datei")
        st.dataframe(
            pd.DataFrame(ergebnisse_pro_datei),
            hide_index=True, use_container_width=True,
        )

        logger.info(
            "GUI-ZIP: Batch fertig — %d geladen, %d abgelehnt, %d in Quarantaene",
            stats["geladen"], stats["abgelehnt"], stats["quarantaene_gesamt"],
        )

    st.stop()


# --- Ab hier: normaler Einzeldatei-Flow -------------------------------------

if st.session_state.temp_pfad is None:
    st.info("Bitte eine Datei zum Import auswaehlen.")
    st.stop()

datei = st.session_state.temp_pfad
st.success(
    f"Datei bereit: **{st.session_state.upload_name}** "
    f"({datei.stat().st_size:,} Byte)"
)
st.caption(
    "💡 Sie koennen diese Datei beliebig oft pruefen. "
    "Erst mit 'In Zieltabelle schreiben' wird sie in die Datenbank uebernommen."
)


# --- Schritt 2: Quelle waehlen (mit "+"-Button) -----------------------------


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
            f"unter Schwellenwert {schwelle:.0%}). Bitte manuell waehlen "
            "oder eine neue Quelle anlegen."
        )
else:
    st.warning(
        "Keine Quelle passt strukturell zu dieser Datei. "
        "Bitte eine neue Quelle anlegen (siehe '+'-Button)."
    )

col_dropdown, col_plus = st.columns([5, 1])
with col_dropdown:
    st.session_state.gewaehlte_quelle = st.selectbox(
        "Quelle:", options=quellen, index=default_index, label_visibility="collapsed",
    )
with col_plus:
    if st.button("➕ Neue Quelle", use_container_width=True):
        st.session_state.neue_quelle_dialog = True
        st.rerun()


# --- Schritt 3: Pruefen (Sandbox) -------------------------------------------


st.header("3. Pruefen")

col_pruefen, col_neu = st.columns([1, 1])
with col_pruefen:
    pruefen = st.button(
        "🔍 Pruefen (ohne Speichern)", type="primary", use_container_width=True
    )
with col_neu:
    if st.button("🔄 Andere Datei", use_container_width=True):
        _session_reset()
        st.rerun()

if pruefen:
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
        "💡 Passen Sie die Datei oder die Konfiguration an und pruefen Sie "
        "erneut. **Nichts wurde in die Datenbank geschrieben.**"
    )
    st.stop()


# --- Fall B: erfolgreich, Vorschau ------------------------------------------


st.header("4. Vorschau")
assert ergebnis.mapping is not None and ergebnis.cfg is not None

st.success(
    "✓ Datei ist gueltig. **Noch nichts in die Datenbank geschrieben** — "
    "die Vorschau zeigt, wie die Daten aussehen wuerden."
)

# Info bei partiellem Modus
if ergebnis.zeilen_quarantaene > 0:
    st.warning(
        f"⚠️ **Partieller Modus:** {ergebnis.zeilen_quarantaene} Zeilen sind "
        f"fehlerhaft und werden NICHT geladen. Diese landen beim Schreiben "
        f"in einer Quarantaene-CSV im Reject-Ordner (mit Fehlergrund)."
    )
    with st.expander(
        f"Details zu den {ergebnis.zeilen_quarantaene} fehlerhaften Zeilen"
    ):
        for d in ergebnis.fehler_details[:100]:
            st.text(d)

st.write(
    f"→ **{len(ergebnis.mapping.saetze)}** Datensaetze "
    f"fuer Zieltabelle **`{ergebnis.cfg.zielsystem.tabelle}`** "
    f"(PK-Konflikt-Modus: `{ergebnis.cfg.zielsystem.pk_konflikt}`)."
)

df = pd.DataFrame(ergebnis.mapping.saetze, columns=ergebnis.mapping.zielfelder)
st.dataframe(df, hide_index=True, use_container_width=True)


# --- Schritt 5: Schreiben (Ernstfall) ---------------------------------------


st.header("5. In die Datenbank schreiben")

st.warning(
    "⚠️ **Ab hier wird es ernst:** Datensaetze werden in die Zieltabelle "
    "eingefuegt, die Datei wird ins Archiv verschoben, der Vorgang wird "
    "protokolliert."
)

schreiben = st.button(
    "✅ In Zieltabelle schreiben", type="primary", use_container_width=True
)

if schreiben:
    logger.info(
        "GUI: Schreibvorgang gestartet — Datei '%s' (Hash %s...), Quelle '%s'",
        st.session_state.upload_name,
        (st.session_state.upload_hash or "")[:12],
        ergebnis.quelle,
    )
    engine.schreibe(ergebnis)

    if ergebnis.erfolg:
        # Erst Quarantaene-CSV schreiben, dann Original ins Archiv
        quarantaene_pfad = None
        if ergebnis.quarantaene_zeilen:
            quarantaene_pfad = schreibe_quarantaene(
                datei, DEFAULT_REJECT, ergebnis.quarantaene_zeilen
            )

        ziel = verschiebe_ins_archiv(datei, DEFAULT_ARCHIVE, ergebnis.quelle)  # type: ignore[arg-type]

        # Erfolgsmeldung zusammenbauen
        msg = f"✅ **Erfolgreich:** {ergebnis.zeilen_geladen} Datensaetze geladen"
        if ergebnis.zeilen_uebersprungen:
            msg += (
                f", {ergebnis.zeilen_uebersprungen} uebersprungen (PK existierte)"
            )
        if ergebnis.zeilen_quarantaene:
            msg += (
                f", {ergebnis.zeilen_quarantaene} in Quarantaene "
                f"(siehe: `{quarantaene_pfad}`)"
            )
        msg += f".\n\nDatei archiviert: `{ziel}`"
        st.success(msg)

        logger.info(
            "GUI: '%s' geladen (%d neu, %d uebersprungen, %d in Quarantaene)",
            st.session_state.upload_name,
            ergebnis.zeilen_geladen,
            ergebnis.zeilen_uebersprungen,
            ergebnis.zeilen_quarantaene,
        )
    else:
        st.error(f"❌ **Fehler beim Schreiben:** {ergebnis.fehler_grund}")
        ziel = verschiebe_ins_reject(
            datei, DEFAULT_REJECT, ergebnis.fehler_grund, ergebnis.fehler_details
        )
        st.info(f"Datei verschoben nach: `{ziel}`")
        logger.info(
            "GUI: '%s' beim Schreiben abgelehnt (%s)",
            st.session_state.upload_name, ergebnis.fehler_grund,
        )

    # Datei ist bereits verschoben — nicht mehr loeschen, nur State leeren.
    st.session_state.temp_pfad = None
    st.session_state.upload_hash = None
    st.session_state.upload_name = None
    st.session_state.ergebnis = None
    st.session_state.gewaehlte_quelle = None