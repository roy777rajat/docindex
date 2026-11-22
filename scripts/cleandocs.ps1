# Simple admin commands. Run from the repo root (needs uv and your AWS credentials):
#   .\scripts\cleandocs.ps1 list                         show all documents with ids
#   .\scripts\cleandocs.ps1 delete doc_xxx               delete one document (or a whole upload: ing_xxx; a ZIP removes its children)
#   .\scripts\cleandocs.ps1 delete doc_xxx -DryRun       show what would be deleted, delete nothing
#   .\scripts\cleandocs.ps1 delete-all                   delete EVERYTHING (asks you to type DELETE ALL)
#   .\scripts\cleandocs.ps1 clean-dry                    count what delete-all would remove (deletes nothing)
param([Parameter(Mandatory = $true, Position = 0)][string]$Command, [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Ids, [switch]$DryRun, [string]$Region = "eu-west-1")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONIOENCODING = "utf-8"
$py = @("run", "--python", "3.12", "--with", "boto3>=1.40", "python", "scripts\cleandocs_admin.py", "--region", $Region)
switch ($Command) {
  "list"      { uv @py list }
  "delete"    { if (-not $Ids) { throw "give a doc_... or ing_... id (see: .\scripts\cleandocs.ps1 list)" }; $x = @("delete") + $Ids; if ($DryRun) { $x += "--dry-run" }; uv @py @x }
  "delete-all" { uv @py delete-all }
  "clean-dry" { uv run --python 3.12 --with "boto3>=1.40" python scripts\clean_all.py --region $Region }
  default     { throw "unknown command '$Command'. Use: list | delete <id> | delete-all | clean-dry" }
}
