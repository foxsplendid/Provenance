# register-sync-tasks.ps1 -- the sync layer is PULL-ONLY; there are no daemons.
#
# The periodic 'Provenance Sync Drain' task and the two at-logon resident watchers
# ('Provenance Vault Watcher' / 'Provenance Openclaw Watcher') have been retired:
# the flow is now run on demand with
#
#     .\sync-run.ps1 -Mode once
#
# which ingests the vault + openclaw drops, scaffolds/assembles session summaries,
# and refreshes the Approvals queue in a single hand-run pass. The only background
# wiring left is the SessionEnd enqueue hook (sessionend-hook.json), which just
# drops a marker for the next pull -- it starts no process and runs no LLM.
#
# Nothing to register. This script defensively removes any tasks left behind by a
# previous (daemon-based) install so re-running it cleans up old wiring.
$ErrorActionPreference = 'Stop'

$TaskPath  = '\Provenance\'
$OldTasks  = @('Provenance Sync Drain', 'Provenance Vault Watcher', 'Provenance Openclaw Watcher')

Write-Host "The sync layer is pull-only -- no scheduled tasks to register."
Write-Host "Run on demand with:  .\sync-run.ps1 -Mode once"
Write-Host ""

foreach ($name in $OldTasks) {
    $t = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
        Write-Host "removed stale task: $TaskPath$name"
    }
}
