"""
Verzeichnis-Watcher fuer das Ingest-Verzeichnis.

Loest die Verarbeitung aus, sobald eine neue Datei erscheint —
unabhaengig davon, wie sie dorthin gelangt ist (FTP-Puller,
manueller Upload, Netzlaufwerk-Sync etc.).

Das ist die Kern-Entkopplung der Architektur: die Pipeline weiss
nicht und muss nicht wissen, wer die Datei geliefert hat.
"""

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class IngestHandler(FileSystemEventHandler):
    def __init__(self, engine) -> None:  # type: ignore[no-untyped-def]
        self.engine = engine

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        file_path = Path(str(event.src_path))
        logger.info("Neue Datei erkannt: %s", file_path.name)
        # Kurz warten, damit die Datei fertig geschrieben ist
        time.sleep(0.5)
        self.engine.process_file(file_path)


def watch_directory(ingest_dir: Path, engine) -> None:  # type: ignore[no-untyped-def]
    """Ingest-Verzeichnis dauerhaft ueberwachen, bis Strg-C."""
    handler = IngestHandler(engine)
    observer = Observer()
    observer.schedule(handler, str(ingest_dir), recursive=False)
    observer.start()
    logger.info("Ueberwachung gestartet: %s", ingest_dir)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ueberwachung wird beendet.")
    finally:
        observer.stop()
        observer.join()
