# unregister-sync-tasks.ps1 -- remove any sync-layer scheduled tasks (rollback).
#
# The sync layer is pull-only now (see register-sync-tasks.ps1); this script exists
# to tear down the retired daemon tasks if an older install registered them. It does
# NOT touch your settings.json SessionEnd hook or any data under sync-state/.
#
# Idempotent: a task that is already absent is reported "absent (ok)" and skipped.
$ErrorActionPreference = 'Stop'

$TaskPath  = '\Provenance\'
$TaskNames = @('Provenance Sync Drain', 'Provenance Vault Watcher', 'Provenance Openclaw Watcher')

foreach ($name in $TaskNames) {
    $t = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
        Write-Host "unregistered: $TaskPath$name"
    }
    else {
        Write-Host "absent (ok):  $TaskPath$name"
    }
}
