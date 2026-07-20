# Event/sync-layer wiring

This runbook enables the optional local session-capture loop. Capture is
host-specific and opt-in; processing remains an explicit on-demand pull.

## Safety model

- The hook only appends one event to `%PROVENANCE_HOME%\sync-state\queue.jsonl`.
- The hook does not summarize, approve, call a model, or start a resident process.
- `prov-sync-pull` previews by default. `--run` is a separate write authorization.
- High-value claims remain drafts until the user approves them.
- `sync-state/` is local protected state. Do not publish, sync, or index it.

## Prerequisites

1. Install Provenance in a dedicated environment.
2. Create a data root outside the source checkout.
3. Configure a Markdown workspace with a registered
   `Projects/synthetic-project.md` note when testing project resolution.
4. Keep the workspace and data root separate and non-nested.

Use environment variables or replace the placeholders below:

| Placeholder | Meaning |
|---|---|
| `%PROVENANCE_HOME%` | Local Provenance data root |
| `%PROVENANCE_PYTHON%` | Python executable from the Provenance environment |
| `%USERPROFILE%` | Current Windows user profile |
| `<provenance-checkout>` | Source checkout containing `harden/` |
| `<vault>` | Markdown workspace root |

## Configure the on-demand runner

Copy the public template and review the resulting local file:

```powershell
Set-Location <provenance-checkout>\harden
Copy-Item .\sync-env.example.ps1 .\sync-env.ps1
```

`sync-env.ps1` is local configuration and must remain ignored. The template
resolves the checkout and virtual-environment Python automatically; set
`PROVENANCE_HOME`, `PROVENANCE_VAULT`, or `PROVENANCE_OPENCLAW_INBOX` when the
defaults do not match your host.

Preview the public pull boundary before any write:

```powershell
prov-sync-pull --provenance-home %PROVENANCE_HOME% --workspace <vault> --json
```

Run the reviewed local pass only after explicit authorization:

```powershell
prov-sync-pull --provenance-home %PROVENANCE_HOME% --workspace <vault> --run --json
```

The legacy helper remains available for an operator who has already created
`harden\sync-env.ps1`:

```powershell
Set-Location <provenance-checkout>\harden
.\sync-run.ps1 -Mode once
```

No current helper registers scheduled tasks or a daemon.

## Optional Claude Code SessionEnd capture

The merge template is
[`harden/sessionend-hook.json`](../harden/sessionend-hook.json). Replace
`%PROVENANCE_HOME%` and `%PROVENANCE_PYTHON%` with reviewed absolute local
paths, then manually merge only its `env` and `SessionEnd` entries into the host
settings.

The command must invoke Python directly:

```text
"%PROVENANCE_PYTHON%" -m provenance.synclayer.enqueue
```

The accepted payload fields are:

```text
session_id
transcript_path
cwd
hook_event_name
reason
```

Unknown fields are ignored. The worker sanitizes turns, strips the absolute
working directory from the scaffold, and binds every fill to local state.

Do not register `UserPromptSubmit`, `PostToolUse`, or a periodic drain for this
workflow. They create duplicate events or hidden write timing without improving
the closeout contract.

## Synthetic capture check

Use only synthetic identifiers when validating a hook:

```json
{
  "session_id": "synthetic-session",
  "transcript_path": "%USERPROFILE%\\.agent\\sessions\\synthetic-session.jsonl",
  "cwd": "%USERPROFILE%\\Projects\\synthetic-project",
  "hook_event_name": "SessionEnd",
  "reason": "test"
}
```

Confirm that the hook returns promptly and that the next pull preview reports
aggregate work. Do not publish the queue or transcript.

## Interactive summary fill

Use the public commands rather than opening internal pending paths:

```powershell
prov-sync-pending --provenance-home %PROVENANCE_HOME% --json
prov-sync-pending <summary-id> --provenance-home %PROVENANCE_HOME% --json
prov-sync-fill <summary-id> --provenance-home %PROVENANCE_HOME%
```

The `skills/session-summary/SKILL.md` workflow produces a `summary-fill/1.0`
object from one sanitized scaffold. An unresolved project must go through
explicit project resolution; it must not produce a `session-summary/1.0`.

After a fill is accepted, rerun the pull preview. Obtain separate authorization
before `--run` applies the low-risk timeline or refreshes the approval queue.

## Recursion guard

Agent sessions launched while processing pending work must set:

```text
PROV_SYNC_NO_ENQUEUE=1
```

The enqueue entrypoint treats this as a no-op, preventing the workflow from
capturing its own maintenance session.

## Read-only diagnostics

Prefer public, path-suppressing commands:

```powershell
prov-sync-pull --provenance-home %PROVENANCE_HOME% --workspace <vault> --json
prov-sync-pending --provenance-home %PROVENANCE_HOME% --json
prov-sync-unresolved --provenance-home %PROVENANCE_HOME% --json
```

Use `--show-paths` only during an attended local mapping review. Never attach raw
reports containing local paths or session identifiers to a public issue.

## Rollback

1. Remove the manually merged `SessionEnd` block from the host settings.
2. Stop invoking `sync-run.ps1` or `prov-sync-pull --run`.
3. If a retired daemon-based install left scheduled tasks, run
   `harden\unregister-sync-tasks.ps1`.
4. Preserve `sync-state/` unless the user explicitly authorizes deletion of that
   exact local data tree.

