# harden/ — optional local sync wiring

These templates enable the already-tested event/sync layer without registering
a daemon. Capture is opt-in and enqueue-only; processing runs only when the user
invokes `prov-sync-pull --run` or `sync-run.ps1 -Mode once`.

| File | Purpose |
|---|---|
| `sync-env.example.ps1` | Public template. Copy to ignored `sync-env.ps1` and review the local data/workspace paths. |
| `sync-run.ps1` | On-demand local runner for ingest, worker, and approvals. |
| `sessionend-hook.json` | Claude Code `SessionEnd` merge template using `%PROVENANCE_HOME%` and `%PROVENANCE_PYTHON%` placeholders. |
| `register-sync-tasks.ps1` | Removes stale tasks from retired daemon-based installs; registers nothing. |
| `unregister-sync-tasks.ps1` | Idempotent rollback helper for those retired tasks. |

## Quick start

```powershell
Set-Location <provenance-checkout>
uv sync --locked
Set-Location .\harden
Copy-Item .\sync-env.example.ps1 .\sync-env.ps1
# Review sync-env.ps1, then run one explicit local pass:
.\sync-run.ps1 -Mode once
```

Merge `sessionend-hook.json` manually only if Claude Code capture is desired.
Replace both placeholders with reviewed absolute local paths. The hook starts no
daemon and calls no model.

For interactive fills, follow
`skills/session-summary/SKILL.md` and use the public `prov-sync-pending` /
`prov-sync-fill` commands. See
[`docs/sync-layer-wiring.md`](../docs/sync-layer-wiring.md) for the complete
safety and rollback procedure.

