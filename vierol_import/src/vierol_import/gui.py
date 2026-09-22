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
    ("schnellimport_schreiben", False),
    ("zip_dateien", None),         # Liste EntpackteDatei nach ZIP-Upload
    ("zip_verzeichnis", None),     # Temp-Ordner mit den entpackten Dateien
    ("zip_batch_status", None),    # dict mit Erfolgs/Fehler-Zaehlern
    ("zip_wahl", None),            # dict {zip-pfad: gewaehlte quelle}
    ("zip_pruefungen", None),      # dict {zip-pfad: VerarbeitungsErgebnis}
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
    st.session_state.zip_wahl = None
    st.session_state.zip_pruefungen = None


def _zip_verzeichnis_aufraeumen() -> None:
    """Loescht den entpackten ZIP-Ordner mitsamt allen Dateien."""
    import shutil
    zv = st.session_state.zip_verzeichnis
    if zv and zv.exists():
        shutil.rmtree(zv, ignore_errors=True)


def _zeige_fehler_gruppiert(fehler_details: list[str]) -> None:
    """Fehlermeldungen gruppieren, damit '150x ungueltiges Datum' nicht
    als 150 einzelne Zeilen erscheint. Gruppiert nach Spalte + Grund."""
    from collections import Counter
    import re

    # Struktur der Details:
    #   "Zeile 47, Spalte 'oeno': '9.91E+40' — passt nicht zu Typ 'string' / Muster '...'"
    # Wir extrahieren Spalte + Grund als Kategorie.
    kategorien: Counter[str] = Counter()
    beispiele: dict[str, str] = {}

    for zeile in fehler_details:
        # Spalten-Fehler
        m = re.match(r"Zeile \d+, Spalte '([^']+)': '([^']*)' — (.+)", zeile)
        if m:
            spalte, wert, grund = m.groups()
            kat = f"Spalte '{spalte}': {grund}"
            kategorien[kat] += 1
            beispiele.setdefault(kat, f"Beispiel: '{wert}'")
            continue
        # Zeilen-Fehler ohne Spalte (z. B. Spaltenanzahl)
        m2 = re.match(r"Zeile \d+: (.+)", zeile)
        if m2:
            grund = m2.group(1)
            kategorien[grund] += 1
            continue
        # Fallback: ganze Meldung als Kategorie
        kategorien[zeile] += 1

    st.caption(f"{len(kategorien)} unterschiedliche Fehlerkategorie(n):")
    df = pd.DataFrame([
        {"Anzahl": anz, "Kategorie": kat, "Beispiel": beispiele.get(kat, "")}
        for kat, anz in kategorien.most_common()
    ])
    st.dataframe(df, hide_index=True, use_container_width=True)


def _schreibe_batch(bereit: list, engine) -> None:
    """Alle 'bereit'-Dateien nacheinander schreiben."""
    stats = {"geladen": 0, "fehler": 0, "quarantaene_gesamt": 0}
    pbar = st.progress(0.0, text="Schreibe...")

    for i, (ed, erg) in enumerate(bereit, start=1):
        pbar.progress(i / len(bereit),
                      text=f"[{i}/{len(bereit)}] {ed.original_pfad}")
        engine.schreibe(erg)

        if erg.erfolg:
            if erg.quarantaene_zeilen:
                schreibe_quarantaene(
                    ed.pfad, DEFAULT_REJECT, erg.quarantaene_zeilen
                )
                stats["quarantaene_gesamt"] += erg.zeilen_quarantaene
            verschiebe_ins_archiv(ed.pfad, DEFAULT_ARCHIVE, erg.quelle)  # type: ignore[arg-type]
            stats["geladen"] += 1
            logger.info("GUI-ZIP: '%s' geladen (%d neu, %d Quarantaene)",
                        ed.original_pfad, erg.zeilen_geladen, erg.zeilen_quarantaene)
        else:
            verschiebe_ins_reject(
                ed.pfad, DEFAULT_REJECT, erg.fehler_grund, erg.fehler_details
            )
            stats["fehler"] += 1
            logger.info("GUI-ZIP: '%s' beim Schreiben abgelehnt (%s)",
                        ed.original_pfad, erg.fehler_grund)

    pbar.empty()

    if stats["fehler"] == 0:
        st.success(
            f"✅ Alle {stats['geladen']} Dateien geschrieben."
            + (
                f" ({stats['quarantaene_gesamt']} Zeilen in Quarantaene)"
                if stats["quarantaene_gesamt"] else ""
            )
        )
    else:
        st.warning(
            f"Fertig: {stats['geladen']} geschrieben, {stats['fehler']} "
            "beim Schreiben abgelehnt."
        )

    # Session zuruecksetzen -- neuer Upload
    st.session_state.zip_pruefungen = None
    st.session_state.zip_wahl = None


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
        f"({sum(e.groesse for e in zd) / (1024 * 1024):.1f} MB gesamt)"
    )

    # Klassifikation vor allen anderen Schritten -----------------------------
    # Alle Dateien werden gleich behandelt: pro Zeile Dropdown + Pruef-Button,
    # egal wie sicher die automatische Zuordnung war. So kann der User bei
    # jedem Vorschlag ueberschreiben, auch wenn der Score 100% ist
    # (Beispiel: gleiche Struktur, andere Zieltabelle).
    NICHT_IMPORTIEREN = "— nicht importieren —"
    dropdown_optionen = [NICHT_IMPORTIEREN] + quellen

    dateien_info = []  # Liste aus (EntpackteDatei, ranking, default_quelle)

    zaehler = {"sicher": 0, "unsicher": 0, "kein_match": 0}
    for ed in zd:
        ranking = klassifiziere(ed.pfad, engine.configs)
        bester = ranking.bester
        if bester is None:
            default_quelle = NICHT_IMPORTIEREN
            zaehler["kein_match"] += 1
        else:
            default_quelle = bester.quelle
            schwelle = engine.configs[bester.quelle].klassifikation.schwellenwert
            if ranking.ist_eindeutig(schwelle):
                zaehler["sicher"] += 1
            else:
                zaehler["unsicher"] += 1
        dateien_info.append((ed, ranking, default_quelle))

    # Uebersicht ------------------------------------------------------------
    st.subheader("Uebersicht")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("✓ sicher", zaehler["sicher"])
    with col2:
        st.metric("? unsicher", zaehler["unsicher"])
    with col3:
        st.metric("✗ kein Match", zaehler["kein_match"])

    # Speicher fuer Wahl pro Datei
    if st.session_state.zip_wahl is None:
        st.session_state.zip_wahl = {}
    for ed, _, default_quelle in dateien_info:
        if ed.original_pfad not in st.session_state.zip_wahl:
            st.session_state.zip_wahl[ed.original_pfad] = default_quelle

    # Pruef-Speicher initialisieren
    if st.session_state.zip_pruefungen is None:
        st.session_state.zip_pruefungen = {}

    def _pruefe_datei(ed_obj, quelle: str) -> None:
        """Eine einzelne Datei pruefen (Sandbox, kein Schreiben)."""
        with st.spinner(f"Pruefe {ed_obj.original_pfad} ..."):
            st.session_state.zip_pruefungen[ed_obj.original_pfad] = (
                engine.verarbeite_mit_quelle(ed_obj.pfad, quelle)
            )

    # --- Datei-Liste mit Dropdown + Pruef-Button pro Zeile -----------------
    st.markdown("### Dateien")
    st.caption(
        "Fuer jede Datei die Quelle bestaetigen oder aendern. "
        "'🔍 Pruefen' laesst eine Datei einzeln durch die Sandbox laufen; "
        "'Schreiben' unten arbeitet alle mit gueltiger Quelle ab."
    )

    for ed, ranking, _ in dateien_info:
        with st.container(border=True):
            col_datei, col_dropdown, col_pruef = st.columns([3, 2, 1])

            with col_datei:
                st.markdown(f"📄 **{ed.original_pfad}**")
                # Score-Zusammenfassung (max 3 Kandidaten)
                kandidaten = [e for e in ranking.ergebnisse if e.moeglich]
                if kandidaten:
                    score_str = ", ".join(
                        f"{e.quelle} ({e.score:.0%})" for e in kandidaten[:3]
                    )
                    st.caption(f"Vorschlag: {score_str}")
                else:
                    st.caption("Vorschlag: (keine Config passt strukturell)")

                # Pruefstatus anzeigen, sofern gepruefft
                erg = st.session_state.zip_pruefungen.get(ed.original_pfad)
                if erg is not None:
                    if erg.bereit_zum_schreiben:
                        txt = f"✅ bereit ({len(erg.mapping.saetze)} Zeilen)"
                        if erg.zeilen_quarantaene:
                            txt += f" · ⚠️ {erg.zeilen_quarantaene} in Quarantaene"
                        st.caption(txt)
                    else:
                        st.caption(f"❌ {erg.fehler_grund[:80]}")

            with col_dropdown:
                current = st.session_state.zip_wahl.get(
                    ed.original_pfad, NICHT_IMPORTIEREN
                )
                idx = (
                    dropdown_optionen.index(current)
                    if current in dropdown_optionen else 0
                )
                neu_gewaehlt = st.selectbox(
                    f"Quelle fuer {ed.original_pfad}",
                    options=dropdown_optionen,
                    index=idx,
                    key=f"quelle_{ed.original_pfad}",
                    label_visibility="collapsed",
                )
                if neu_gewaehlt != st.session_state.zip_wahl[ed.original_pfad]:
                    # Andere Quelle -> alte Pruefung ungueltig
                    st.session_state.zip_pruefungen.pop(ed.original_pfad, None)
                st.session_state.zip_wahl[ed.original_pfad] = neu_gewaehlt

            with col_pruef:
                quelle_gewaehlt = st.session_state.zip_wahl.get(ed.original_pfad)
                pruef_disabled = (quelle_gewaehlt == NICHT_IMPORTIEREN)
                if st.button(
                    "🔍 Pruefen", key=f"pruef_{ed.original_pfad}",
                    use_container_width=True, disabled=pruef_disabled,
                ):
                    _pruefe_datei(ed, quelle_gewaehlt)
                    st.rerun()

            # Pruef-Details bei geprueffter Datei aufklappbar
            erg = st.session_state.zip_pruefungen.get(ed.original_pfad)
            if erg is not None and erg.bereit_zum_schreiben:
                with st.expander("Vorschau (erste 20)"):
                    st.dataframe(
                        pd.DataFrame(
                            erg.mapping.saetze[:20],
                            columns=erg.mapping.zielfelder,
                        ),
                        hide_index=True, use_container_width=True,
                    )
                if erg.zeilen_quarantaene:
                    with st.expander(
                        f"⚠️ {erg.zeilen_quarantaene} Zeile(n) in Quarantaene"
                    ):
                        _zeige_fehler_gruppiert(erg.fehler_details)
            elif erg is not None:
                with st.expander(
                    f"Fehlerdetails ({len(erg.fehler_details)}, gruppiert)"
                ):
                    _zeige_fehler_gruppiert(erg.fehler_details)

    st.divider()

    # --- Aktions-Buttons ---------------------------------------------------
    zu_schreiben = [
        (ed, st.session_state.zip_wahl[ed.original_pfad])
        for ed, _, _ in dateien_info
        if st.session_state.zip_wahl[ed.original_pfad] != NICHT_IMPORTIEREN
    ]

    col_btn1, col_btn2, col_btn3 = st.columns([2, 1, 1])
    with col_btn1:
        schreiben_alles = st.button(
            f"✅ {len(zu_schreiben)} Datei(en) schreiben "
            "(Pruefung erfolgt automatisch)",
            type="primary", use_container_width=True,
            disabled=(len(zu_schreiben) == 0),
        )
    with col_btn2:
        if st.button("➕ Neue Quelle anlegen", use_container_width=True):
            st.session_state.neue_quelle_dialog = True
            st.rerun()
    with col_btn3:
        if st.button("🔄 Anderes ZIP hochladen", use_container_width=True):
            st.session_state.zip_wahl = None
            st.session_state.zip_pruefungen = None
            _session_reset()
            st.rerun()

    # --- Schreiben: Pruefung + Load + Ergebnis-Report ---------------------
    if schreiben_alles:
        report_erfolge = []     # (name, quelle, tabelle, zeilen, quarantaene)
        report_fehler = []      # (name, grund, details)

        pbar = st.progress(0.0, text="Verarbeite ...")
        for i, (ed, quelle) in enumerate(zu_schreiben, start=1):
            pbar.progress(
                i / len(zu_schreiben),
                text=f"[{i}/{len(zu_schreiben)}] {ed.original_pfad}",
            )
            # Pruefung intern erneut ausfuehren, um immer den frischen
            # Stand zu haben (falls User die Datei zwischenzeitlich nicht
            # per Button gepruefft hat).
            erg = engine.verarbeite_mit_quelle(ed.pfad, quelle)

            if not erg.bereit_zum_schreiben:
                report_fehler.append((
                    ed.original_pfad, erg.fehler_grund, erg.fehler_details,
                ))
                verschiebe_ins_reject(
                    ed.pfad, DEFAULT_REJECT,
                    erg.fehler_grund, erg.fehler_details,
                )
                continue

            # Schreiben
            engine.schreibe(erg)
            if erg.erfolg:
                if erg.quarantaene_zeilen:
                    schreibe_quarantaene(
                        ed.pfad, DEFAULT_REJECT, erg.quarantaene_zeilen
                    )
                verschiebe_ins_archiv(
                    ed.pfad, DEFAULT_ARCHIVE, erg.quelle,  # type: ignore[arg-type]
                )
                report_erfolge.append((
                    ed.original_pfad, erg.quelle, erg.cfg.zielsystem.tabelle,
                    erg.zeilen_geladen, erg.zeilen_quarantaene,
                ))
                logger.info("GUI-ZIP: '%s' geladen (%d neu, %d Quarantaene)",
                            ed.original_pfad, erg.zeilen_geladen,
                            erg.zeilen_quarantaene)
            else:
                report_fehler.append((
                    ed.original_pfad, erg.fehler_grund, erg.fehler_details,
                ))
                verschiebe_ins_reject(
                    ed.pfad, DEFAULT_REJECT,
                    erg.fehler_grund, erg.fehler_details,
                )
                logger.info("GUI-ZIP: '%s' beim Schreiben abgelehnt (%s)",
                            ed.original_pfad, erg.fehler_grund)

        # Kein-Match-Dateien: nur ins Reject (nicht Teil von zu_schreiben)
        for ed, ranking, _ in dateien_info:
            if st.session_state.zip_wahl[ed.original_pfad] != NICHT_IMPORTIEREN:
                continue
            if any(e.moeglich for e in ranking.ergebnisse):
                continue  # User hat bewusst "nicht importieren" gewaehlt
            details = [
                f"{e.quelle}: {e.ko_grund or 'Score '+format(e.score, '.0%')}"
                for e in ranking.ergebnisse
            ]
            verschiebe_ins_reject(
                ed.pfad, DEFAULT_REJECT,
                "Keine Config passt zu dieser Datei", details,
            )

        pbar.empty()

        # --- Ergebnis-Report anzeigen -----------------------------------
        st.subheader("Ergebnis")
        col_ok, col_err = st.columns(2)
        with col_ok:
            st.metric("✅ Erfolgreich", len(report_erfolge))
        with col_err:
            st.metric("❌ Fehlgeschlagen", len(report_fehler))

        if report_erfolge:
            st.markdown("#### ✅ Erfolgreich geladen")
            df_ok = pd.DataFrame([
                {
                    "Datei": n,
                    "Quelle": q,
                    "Zieltabelle": t,
                    "Zeilen geladen": z,
                    "In Quarantaene": qua or "-",
                }
                for n, q, t, z, qua in report_erfolge
            ])
            st.dataframe(df_ok, hide_index=True, use_container_width=True)

        if report_fehler:
            st.markdown("#### ❌ Fehler (Details ausklappbar)")
            for name, grund, details in report_fehler:
                with st.container(border=True):
                    st.markdown(f"**{name}**")
                    st.caption(grund)
                    if details:
                        with st.expander(
                            f"Fehlerdetails ({len(details)} Eintraege, gruppiert)"
                        ):
                            _zeige_fehler_gruppiert(details)

        # Session-Zwischenzustand zuruecksetzen -> User kann neues ZIP laden
        st.session_state.zip_wahl = None
        st.session_state.zip_pruefungen = None

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

schnellimport = st.checkbox(
    "🚀 Schnellimport (ohne Pruefvorschau direkt schreiben)",
    value=False,
    help=(
        "Fuer Routine-Importe: die Datei wird gepruefft und bei Erfolg "
        "sofort geschrieben — ohne Vorschau. Bei Fehlern wird trotzdem "
        "abgebrochen und angezeigt. Verwendbar, wenn Sie den Datentyp "
        "gut kennen."
    ),
)

col_pruefen, col_neu = st.columns([1, 1])
with col_pruefen:
    pruefen_label = (
        "🚀 Schnellimport: pruefen + schreiben" if schnellimport
        else "🔍 Pruefen (ohne Speichern)"
    )
    pruefen = st.button(
        pruefen_label, type="primary", use_container_width=True
    )
with col_neu:
    if st.button("🔄 Andere Datei", use_container_width=True):
        _session_reset()
        st.rerun()

if pruefen:
    st.session_state.ergebnis = engine.verarbeite_mit_quelle(
        datei, st.session_state.gewaehlte_quelle
    )
    # Schnellimport: Bei Erfolg direkt schreiben, Datei archivieren.
    if schnellimport and st.session_state.ergebnis.bereit_zum_schreiben:
        st.session_state.schnellimport_schreiben = True

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

# Schnellimport: Schreiben-Aktion wurde beim Pruefen bereits ausgeloest
# und wird jetzt automatisch bearbeitet.
if st.session_state.schnellimport_schreiben:
    schreiben = True
    st.session_state.schnellimport_schreiben = False
    st.info("🚀 Schnellimport aktiv — schreibe direkt ...")

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