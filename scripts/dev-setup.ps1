# Creates a virtual environment, installs the shared package with test extras, runs unit tests.
# Run from the repo root in PowerShell:  .\scripts\dev-setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e "shared[test]"
.\.venv\Scripts\python.exe -m pytest tests/unit -q
