# Event/sync worker

The event/sync worker turns an opt-in local session-closeout event into a
reviewable project writeback. It is deterministic, resumable, and fail-closed.

## Flow

```text
SessionEnd or explicit local scan
        |
        v
sync-state/queue.jsonl --claim--> inflight
        |
        +-- project unresolved --> remain inflight; expose aggregate action only
        |
        +-- project resolved ----> sanitized scaffold
                                      |
                                      v
                                reviewed fill
                                      |
                                      v
                              session-summary/1.0
                                |             |
                                v             v
                        append-only       approval draft
                          timeline        (high-value)
```

## Components

| Module | Responsibility |
|---|---|
| `synclayer/enqueue.py` | Parse a closeout payload and append one event; never perform authoritative writes |
| `synclayer/scan_agents.py` | Explicitly discover supported local Agent sessions |
| `synclayer/resolver.py` | Map normalized repositories or project hints to registered project IDs |
| `synclayer/worker.py` | Claim, scaffold, assemble and apply under one kernel-backed lock |
| `synclayer/summary.py` | Build the sanitized scaffold and validate/assemble summary objects |
| `synclayer/pending_fill.py` | Expose opaque pending IDs and atomically accept one validated fill |
| `synclayer/progress_log.py` | Append only inside managed project-note markers and journal each mutation |

## State-machine invariants

1. Preview does not claim or write.
2. A queue snapshot is claimed without losing malformed or interrupted work.
3. A project must resolve to a registered, non-empty `project_id` before a
   scaffold exists.
4. The scaffold contains sanitized turns and omits absolute working-directory and
   transcript paths.
5. The fill cannot provide or override identity fields.
6. Existing valid state is idempotent; conflicting state blocks the event.
7. Timeline failure cannot be converted into a false completion credential.
8. Draft identity, project, timeline and provenance must match before an event is
   retired.

## Interactive fill

The package has no headless summarizer and never calls a model. Use public
commands instead of opening protected pending directories:

```text
prov-sync-pending --provenance-home %PROVENANCE_HOME% --json
prov-sync-pending <summary-id> --provenance-home %PROVENANCE_HOME% --json
prov-sync-fill <summary-id> --provenance-home %PROVENANCE_HOME%
```

The `skills/session-summary` workflow prepares a factual `summary-fill/1.0` from
exactly one allowlisted scaffold. Rerun `prov-sync-pull` in preview mode after the
fill is accepted, then obtain separate authorization for `--run`.

## Locks and recovery

The worker and public pull use persistent protocol-marker files backed by
operating-system locks. Process exit releases the kernel lock. If a process fails
during the first marker publication, an empty or partial marker is ambiguous and
must fail closed. Confirm no worker is active, then remove only the affected lock
file under explicit local authorization.

The Windows pending/fill path additionally holds directory handles without share
delete while validating root, pending directory, scaffold and target. Links and
reparse points are rejected.

## Privacy

`sync-state/` is protected local state. It may contain an unredacted cwd, project
decisions and approval drafts. It is ignored by Git, excluded from the public-tree
gate, never returned by MCP and never networked by the package.

