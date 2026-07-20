# Provenance Architecture Specification

| Field | Value |
|---|---|
| Status | Public living contract |
| Product version | 0.17.0 |
| Runtime | Python standard library |
| Primary release target | Windows 10/11 |
| Data root | `PROVENANCE_HOME` |

This document defines the invariants of the public Provenance product. Historical
private development notes and user data are intentionally not part of this spec.

## 1. Product responsibility

Provenance owns local research memory and session-writeback state. It accepts
versioned files and local source records, creates deterministic local indexes and
views, and exposes read-only context. It must not call another Scriptorium
component's internals.

The contract source of truth for cross-product JSON formats is
`scriptorium-suite/scriptorium-spec`.

## 2. Public release scope

Included:

- installable `provenance` Python package;
- local ingestion, privacy transforms, organization, search, Markdown export;
- literature and project-memory ingestion;
- read-only stdio MCP server;
- event/sync queue, resolver, worker, pending/fill boundary, approvals;
- optional clean-room browser producer under `01-capture/`;
- neutral `skills/`, Windows wiring templates, synthetic tests.

Excluded:

- private data, profiles, project notes, queues, databases, aliases and logs;
- package-owned model calls, telemetry, cloud sync and background daemons.

Compatible conversation ZIP ingestion and the optional browser producer are
separate, manual workflows. The Python package never discovers, launches, or
controls the extension.

## 3. Data root

All mutable data resolves beneath an explicit `PROVENANCE_HOME` or the documented
CLI root. Long-lived data must be outside the source checkout.

```text
PROVENANCE_HOME/
  inbox/          normalized working records and raw ZIP backup
  drop/           optional user-selected import queue
  kb/             versioned literature input
  memory/         project, literature and approved-claim snapshots
  output/         deterministic organization views
  _prep/          deterministic Skill inputs
  profile/        local profile artifacts
  vault/          Markdown/Obsidian export
  sync-state/     protected event, pending, draft and journal state
  search-index.db local FTS5 index
```

Every directory above is private by default and ignored by Git.

## 4. Normalized conversation record

Downstream processing accepts a normalized object with stable source identity:

```json
{
  "source": "codex",
  "conversation_id": "synthetic-session",
  "name": "[SYNTHETIC] XQ-17 calibration review",
  "created_at": "2030-01-01T09:00:00Z",
  "updated_at": "2030-01-01T10:00:00Z",
  "chat_messages": [
    {"sender": "human", "text": "Review the synthetic benchmark."},
    {"sender": "assistant", "text": "The synthetic check completed."}
  ]
}
```

Examples must be obviously synthetic. Source-specific parsers may preserve extra
fields, but identity and de-duplication use `(source, conversation_id)`.

## 5. Privacy transform

1. Parse the source into objects.
2. Apply secret/PII redaction to string values, never to serialized JSON text.
3. Load optional aliases from the private data root.
4. If an alias file is malformed, warn loudly; do not describe the output as
   pseudonymized.
5. Write the normalized working record beneath `inbox/`.

The working record remains private. Attachments are original bytes and do not pass
through the text redactor.

## 6. Deterministic derived layers

- `prov-organize` classifies with public generic defaults or a private override.
- `prov-prep` creates bounded digests from sanitized working records.
- `prov-diff` compares deterministic snapshots.
- `prov-search --build` creates a local SQLite FTS5 index.
- `prov-obsidian` exports local Markdown views.
- `prov-mcp` reads these layers over stdio without mutation or network access.

RAG/search results and Agent-generated summaries are candidates, not accepted
research facts.

## 7. Event/sync state machine

```text
event -> queue -> inflight
                    |
                    +-> unresolved project: remain protected, no summary
                    |
                    +-> resolved project -> sanitized scaffold
                                           -> reviewed summary-fill/1.0
                                           -> session-summary/1.0
                                               +-> append-only timeline
                                               +-> high-value draft
                                                   -> human approval
```

Invariants:

- enqueue is append-only and must not perform authoritative writes;
- one worker serializes writes under a kernel-backed lock;
- queue claims are crash-resumable and idempotent;
- a project must be registered and non-empty before scaffold creation;
- the scaffold omits absolute `cwd` and exposes only sanitized turns;
- fill content cannot override summary, project, session or path identity;
- timeline writes occur only inside the managed progress-log markers;
- status, stage, next actions, conclusion and blockers remain drafts until approval;
- failures preserve recoverable state and return a non-success report.

## 8. Public pending/fill boundary

`prov-sync-pending` lists opaque summary IDs and returns exactly one allowlisted
scaffold. It must reject traversal, links/reparse points, malformed identities and
ambiguous targets.

`prov-sync-fill` reads `summary-fill/1.0` from stdin, validates it, re-checks the
bound target under the worker lock and atomically publishes it. A fill may contain:

- required `timeline` with 2-6 factual items;
- optional `status`, `stage`, `next_actions`, `conclusion`, `blocked_by`,
  `confidence` when supported by the scaffold.

It must not contain identity, timestamp or path fields.

## 9. Pull contract

`prov-sync-pull` requires explicit, existing workspace and data roots. The two
roots must be separate and non-nested.

- default: write-free preview;
- `--run`: one lock-protected local pass;
- `--capabilities --json`: stable read-only capability report;
- exit `0`: expected work/backlog without safety failure;
- exit `1`: partial or conservatively blocked pass;
- exit `2`: no trustworthy plan could be formed.

Reports suppress absolute paths and session identities by default.

## 10. Skills

`skills/provenance-profile` and `skills/session-summary` are host-executed
instructions. They must:

- use only the explicit local data root or one allowlisted scaffold;
- make no network call and create no telemetry;
- avoid raw transcripts unless the profile workflow explicitly needs a missing
  piece of evidence from the sanitized working layer;
- keep outputs local and uncommitted;
- preserve uncertainty and require evidence for claims.

## 11. Public-tree gate

CI must reject:

- known private fixture names or historical local paths;
- non-example email addresses or credential-shaped literals;
- runtime data, databases, JSONL logs, archives and private configuration;
- local Agent/governance state and audit scratch files;
- unreviewed browser adapters, vendored JavaScript, or generated extension archives.

The test suite and examples use the XQ-17 synthetic universe, reserved example
domains and `C:\ScriptoriumDemo\...` paths.

## 12. Verification

```text
python -m unittest discover -s tests -v
python tools/check_public_tree.py
python -B 01-capture/tests/test_extension.py
quick_validate.py skills/provenance-profile
quick_validate.py skills/session-summary
```

Linux and Windows run the extension boundary tests. Linux runs the full Python
suite; Windows additionally exercises lock, reparse-point, pending/fill and public
pull behavior.
