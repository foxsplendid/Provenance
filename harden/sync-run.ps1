# sync-run.ps1 -- the on-demand pull entrypoint for the sync layer (hand-run).
#
#   .\sync-run.ps1 -Mode once       # ingest vault + openclaw, then one drain pass
#   .\sync-run.ps1 -Mode drain      # scaffold -> assemble any already-filled -> approvals
#
# There are no daemons and no headless summarizer: the per-session fill.json is now
# written IN-SESSION by the provenance-review skill (the agent reads the scaffold and
# writes the fill). A pass scaffolds new sessions, assembles any scaffolds the skill
# has already filled, and refreshes the Approvals queue -- nothing auto-summarizes
# between.
#
# It exports the config from sync-env.ps1 into the process env so every
# `python -m provenance...` child resolves PROVENANCE_HOME / VAULT / etc., then
# dispatches. Uses `python -m` (not the prov-* console scripts) so it does not
# depend on the editable install's wrappers being current.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('once', 'drain')]
    [string]$Mode
)
$ErrorActionPreference = 'Stop'
$cfg = Join-Path $PSScriptRoot 'sync-env.ps1'
if (-not (Test-Path $cfg)) {
    Write-Warning "sync-env.ps1 not found -- falling back to sync-env.example.ps1. Copy it to sync-env.ps1 and edit your paths (or set PROVENANCE_HOME / PROVENANCE_VAULT env vars)."
    $cfg = Join-Path $PSScriptRoot 'sync-env.example.ps1'
}
. $cfg

# Export config to the process env -- child `python -m ...` calls read these.
$env:PROVENANCE_HOME           = $ProvenanceHome
$env:PROVENANCE_VAULT          = $ProvenanceVault
$env:PROVENANCE_OPENCLAW_INBOX = $OpenclawInbox

function Invoke-Py { param([string[]]$ModuleArgs) & $Python @ModuleArgs }

switch ($Mode) {
    'drain' {
        # scaffold -> assemble any already-filled -> approvals. The worker is
        # lockfile-guarded (a concurrent run just reports "busy" and exits 0).
        # The first pass scaffolds new sessions; the second assembles + applies any
        # scaffolds the skill has already filled in-session. List what is still
        # awaiting a fill so the skill/user can see the backlog.
        Invoke-Py @('-m', 'provenance.synclayer.worker', '--run')
        Invoke-Py @('-m', 'provenance.synclayer.summarize_driver')   # list-only: pending scaffolds
        Invoke-Py @('-m', 'provenance.synclayer.worker', '--run')
        # Refresh the Approvals queue so the high-value claims the apply step just
        # staged as drafts surface for the user to tick. Idempotent and edit-safe:
        # regenerate preserves prior ticks/edits and drops applied items. Nothing
        # is committed here -- approval stays the one manual touch.
        if (Test-Path $ProvenanceVault) {
            Invoke-Py @('-m', 'provenance.synclayer.approvals', 'generate', '--run', '--vault', $ProvenanceVault)
        }
    }
    'once' {
        # On-demand one-shot: ingest vault notes + openclaw drops, then one drain pass
        # (worker -> list pending -> worker -> approvals). Run by hand to fold recent
        # work into memory. The pull-model entrypoint that replaces the periodic drain.
        if (Test-Path $ProvenanceVault) { Invoke-Py @('-m', 'provenance.ingest.notes', $ProvenanceVault) }
        if (Test-Path $OpenclawInbox)   { Invoke-Py @('-m', 'provenance.ingest.openclaw', $OpenclawInbox) }
        Invoke-Py @('-m', 'provenance.synclayer.worker', '--run')
        Invoke-Py @('-m', 'provenance.synclayer.summarize_driver')   # list-only: pending scaffolds
        Invoke-Py @('-m', 'provenance.synclayer.worker', '--run')
        if (Test-Path $ProvenanceVault) {
            # apply any ticked Approvals first, then refresh the queue so applied
            # items drop off.
            Invoke-Py @('-m', 'provenance.synclayer.approvals', 'approve', '--run', '--vault', $ProvenanceVault)
            Invoke-Py @('-m', 'provenance.synclayer.approvals', 'generate', '--run', '--vault', $ProvenanceVault)
        }
    }
}
