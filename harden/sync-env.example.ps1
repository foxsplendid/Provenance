# sync-env.example.ps1 -- public template for local sync-layer wiring.
#
# Run "uv sync --locked", copy this file to sync-env.ps1, and review the
# resulting local paths. sync-env.ps1 is ignored and must never be committed.
# Environment variables override every fallback below.
#
# PROVENANCE_HOME           -> queue, worker, drafts, and local memory
# PROVENANCE_VAULT          -> project notes and approval queue
# PROVENANCE_OPENCLAW_INBOX -> optional least-trust drop directory
# PROVENANCE_PYTHON         -> interpreter used by the on-demand runner

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DefaultSuiteRoot = Join-Path $env:USERPROFILE 'Research\Scriptorium'

$ProvenanceHome = if ($env:PROVENANCE_HOME) {
    $env:PROVENANCE_HOME
} else {
    Join-Path $DefaultSuiteRoot 'provenance-data'
}
$ProvenanceVault = if ($env:PROVENANCE_VAULT) {
    $env:PROVENANCE_VAULT
} else {
    Join-Path $DefaultSuiteRoot 'workspace'
}
$OpenclawInbox = if ($env:PROVENANCE_OPENCLAW_INBOX) {
    $env:PROVENANCE_OPENCLAW_INBOX
} else {
    Join-Path $ProvenanceVault '_openclaw-inbox'
}
$Python = if ($env:PROVENANCE_PYTHON) {
    $env:PROVENANCE_PYTHON
} else {
    Join-Path $ProjectRoot '.venv\Scripts\python.exe'
}

