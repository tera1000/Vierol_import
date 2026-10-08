# =============================================================================
# erstelle_abgabe.ps1
# -----------------------------------------------------------------------------
# Erstellt eine saubere Abgabe-ZIP des vierol_import-Projekts.
#
# Was das Skript macht:
#   1. Lokale Build-/Laufzeit-Artefakte loeschen (egg-info, Logs, Cache)
#   2. Eine saubere Kopie OHNE .git, .venv, Caches, Artefakte erstellen
#   3. Diese Kopie als ZIP packen
#   4. Die ZIP verifizieren (keine unerwuenschten Elemente drin)
#   5. Den temporaeren Kopie-Ordner wieder entfernen
#
# Verwendung:
#   - Dieses Skript IM Projekt-Root ablegen (neben pyproject.toml)
#   - PowerShell im Projekt-Root oeffnen
#   - Ausfuehren:  .\erstelle_abgabe.ps1
#
# Falls PowerShell die Ausfuehrung blockiert ("Skriptausfuehrung deaktiviert"):
#   powershell -ExecutionPolicy Bypass -File .\erstelle_abgabe.ps1
# =============================================================================

$ErrorActionPreference = "Stop"

# --- Konfiguration -----------------------------------------------------------
$ProjektName   = "vierol_import"
$KopieName     = "vierol_import_abgabe"
$ZipName       = "vierol_import_abgabe.zip"

# Verzeichnisse, die NICHT in die Abgabe gehoeren
$AusschlussVerzeichnisse = @(
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".vscode",
    "*.egg-info"
)

# Dateien, die NICHT in die Abgabe gehoeren
$AusschlussDateien = @(
    "*.pyc",
    "*.sqlite",
    "*.sqlite-journal",
    "*.log.jsonl",
    "*.log"
)

# =============================================================================

Write-Host ""
Write-Host "=== Abgabe-Bereinigung fuer $ProjektName ===" -ForegroundColor Cyan
Write-Host ""

# --- Schritt 0: Sicherstellen, dass wir im Projekt-Root sind -----------------
if (-not (Test-Path "pyproject.toml")) {
    Write-Host "FEHLER: pyproject.toml nicht gefunden." -ForegroundColor Red
    Write-Host "Bitte dieses Skript im Projekt-Root ausfuehren (neben pyproject.toml)." -ForegroundColor Red
    exit 1
}

# Wir arbeiten vom Elternverzeichnis aus, damit robocopy sauber kopieren kann
$ProjektPfad = (Get-Location).Path
$ElternPfad  = Split-Path $ProjektPfad -Parent
$ProjektOrdnerName = Split-Path $ProjektPfad -Leaf

Write-Host "Projekt-Root: $ProjektPfad" -ForegroundColor Gray
Write-Host ""

# --- Schritt 1: Lokale Artefakte im Original-Projekt loeschen ----------------
Write-Host "[1/5] Lokale Build-/Laufzeit-Artefakte loeschen ..." -ForegroundColor Yellow

# egg-info (Build-Artefakt von 'pip install -e')
Get-ChildItem -Path . -Recurse -Directory -Filter "*.egg-info" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "    loesche $($_.FullName)" -ForegroundColor DarkGray
        Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
    }

# __pycache__-Ordner
Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
    }

# alte Log-Dateien (inkl. dem "noch nicht implementiert"-Stand)
Get-ChildItem -Path . -Recurse -File -Include "*.log.jsonl", "*.log" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "    loesche $($_.Name)" -ForegroundColor DarkGray
        Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue
    }

# lokale SQLite-Test-Datenbanken
Get-ChildItem -Path . -Recurse -File -Include "*.sqlite", "*.sqlite-journal" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "    loesche $($_.Name)" -ForegroundColor DarkGray
        Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue
    }

Write-Host "    fertig." -ForegroundColor Green
Write-Host ""

# --- Schritt 2: Saubere Kopie erstellen --------------------------------------
Write-Host "[2/5] Saubere Kopie erstellen (ohne .git, .venv, Caches) ..." -ForegroundColor Yellow

$KopiePfad = Join-Path $ElternPfad $KopieName

# Falls eine alte Kopie herumliegt: weg damit
if (Test-Path $KopiePfad) {
    Remove-Item -Recurse -Force $KopiePfad
}

# robocopy: /E = inkl. Unterordner (auch leere), /XD = Verzeichnisse aus,
# /XF = Dateien aus, /NFL /NDL /NJH /NJS = weniger Log-Rauschen
$roboArgs = @(
    $ProjektPfad,
    $KopiePfad,
    "/E",
    "/XD"
) + $AusschlussVerzeichnisse + @("/XF") + $AusschlussDateien + @(
    "/NFL", "/NDL", "/NJH", "/NJS"
)

robocopy @roboArgs | Out-Null

# robocopy-Exitcodes 0-7 sind Erfolg (8+ ist Fehler)
if ($LASTEXITCODE -ge 8) {
    Write-Host "FEHLER: robocopy fehlgeschlagen (Exitcode $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

Write-Host "    kopiert nach: $KopiePfad" -ForegroundColor Green
Write-Host ""

# --- Schritt 3: ZIP packen ---------------------------------------------------
Write-Host "[3/5] ZIP packen ..." -ForegroundColor Yellow

$ZipPfad = Join-Path $ElternPfad $ZipName

if (Test-Path $ZipPfad) {
    Remove-Item -Force $ZipPfad
}

Compress-Archive -Path $KopiePfad -DestinationPath $ZipPfad -Force

Write-Host "    erstellt: $ZipPfad" -ForegroundColor Green
Write-Host ""

# --- Schritt 4: ZIP verifizieren ---------------------------------------------
Write-Host "[4/5] ZIP auf unerwuenschte Inhalte pruefen ..." -ForegroundColor Yellow

$PruefPfad = Join-Path $ElternPfad "_pruef_temp"
if (Test-Path $PruefPfad) {
    Remove-Item -Recurse -Force $PruefPfad
}
Expand-Archive -Path $ZipPfad -DestinationPath $PruefPfad -Force

$Problemfunde = Get-ChildItem -Recurse -Force $PruefPfad | Where-Object {
    $_.Name -eq ".git" -or
    $_.Name -eq ".venv" -or
    $_.Name -like "*.egg-info" -or
    $_.Name -like "*.log.jsonl" -or
    $_.Name -like "*.sqlite" -or
    $_.Name -eq "__pycache__"
}

Remove-Item -Recurse -Force $PruefPfad

if ($Problemfunde) {
    Write-Host "    WARNUNG: Folgende unerwuenschte Elemente sind noch in der ZIP:" -ForegroundColor Red
    $Problemfunde | ForEach-Object { Write-Host "      - $($_.FullName)" -ForegroundColor Red }
} else {
    Write-Host "    sauber — keine unerwuenschten Elemente gefunden." -ForegroundColor Green
}
Write-Host ""

# --- Schritt 5: Temporaeren Kopie-Ordner entfernen ---------------------------
Write-Host "[5/5] Temporaeren Kopie-Ordner aufraeumen ..." -ForegroundColor Yellow
Remove-Item -Recurse -Force $KopiePfad
Write-Host "    fertig." -ForegroundColor Green
Write-Host ""

# --- Abschluss ---------------------------------------------------------------
$ZipGroesse = [math]::Round((Get-Item $ZipPfad).Length / 1MB, 2)
Write-Host "=== FERTIG ===" -ForegroundColor Cyan
Write-Host "Abgabe-ZIP: $ZipPfad" -ForegroundColor White
Write-Host "Groesse:    $ZipGroesse MB" -ForegroundColor White
Write-Host ""
Write-Host "Diese ZIP kann an den Pruefer gesendet werden." -ForegroundColor Green
Write-Host ""