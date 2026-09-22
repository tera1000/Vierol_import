"""Config-Catalog-Layer: Laden und Validieren der Quellen-Konfigurationen."""

from vierol_import.catalog.reader import CatalogLoadResult, load_catalog
from vierol_import.catalog.meta_schema import QuellenConfig

__all__ = ["CatalogLoadResult", "load_catalog", "QuellenConfig"]