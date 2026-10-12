# Provenance Architecture Specification

| Field | Value |
|---|---|
| Status | Public living contract |
| Product version | 0.18.0 |
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
- literature, literature-reading artifact, and project-memory ingestion;
- bounded project Context Capsules through the CLI and read-only stdio MCP
  server;
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
  memory/         project, literature, research-artifact and approved-claim snapshots
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

### 6.1 Research-artifact ingestion

`prov-ingest-research` accepts only user-supplied files and directories. It does
not discover personal folders, modify source files, or retain source paths. The
0.18.0 implementation consumes exactly these scriptorium-spec contracts:

- `parsed-paper/1.0`;
- `reading-note/1.0`;
- `review/1.0`;
- `lineage-graph/1.0`.

All selected inputs are validated and sanitized before one deterministic merge
into `memory/research-artifacts.json`. Records receive stable content identities;
the load-merge-write transaction is protected by a kernel-backed lock, uses an
atomic replacement, and is idempotent for unchanged records.

Parsed-paper and reading-note identities preserve the exact Better BibTeX
citekey. Review and lineage identities include their `created` value, so a later
snapshot of the same direction is retained while the same direction and
timestamp remain idempotent.

Input traversal and the destination store boundary fail closed when any path
component is a symbolic link, junction or Windows reparse point, including
ancestors and nested entries. Input bytes are read from one verified file
descriptor and file identity is checked before and after the read. The target
and kernel-lock identities are rechecked around the load-merge-write transaction.
Absolute Windows, UNC and POSIX paths and `file:` URIs are redacted before
persistence and search indexing. Read, parse, lock and write failures return
bounded messages that do not echo source or destination paths.

### 6.2 Context Capsule

`prov-context --project PROJECT_ID` and MCP `get_context_capsule` return the same
deterministic, read-only project resume view. The capsule:

- reads human-authored project fields and explicitly approved claims as project
  state;
- includes recent auto-applied timeline entries only as low-risk progress, not
  approved claims;
- includes related literature-reading artifacts and their gap/priority-reading
  leads only as reference material, not approved claims;
- excludes raw conversations, pending drafts, rejected claims, source-path
  fields and unrelated research artifacts;
- omits absolute local paths from all returned text;
- is bounded to 8,000 characters in Markdown and 20,000 characters in the JSON
  envelope, with explicit truncation metadata.

The trust envelope is part of the public output:

```json
{
  "project_state": "human_or_approved",
  "recent_progress": "auto_applied_low_risk_not_approved_claims",
  "research_artifacts": "reference_only_not_approved_claims"
}
```

Neither the CLI nor the MCP method mutates project or memory state.

### 6.3 Experiment evidence candidate registry

`prov-ingest-evidence` consumes explicitly selected `experiment-run/1.x` and
`claim-evidence/1.x` files. Its default operation is a zero-write preview;
the CLI accepts `--run` only together with the matching preview `--plan-id`.
It recomputes that same plan under the write lock before one atomic merge into
the path-free, rebuildable `memory/evidence-registry.json`.

The registry preserves additive fields and the actual minor version. A semantic
SHA-256 source fingerprint is computed from the strict, validated input before
privacy redaction, while only the redacted content is persisted. A second
content fingerprint covers that persisted object. Every strict read recomputes
the digest and reapplies the official local-path/secret sanitizer; a changed
digest or non-idempotent privacy result fails closed before re-ingest. This
prevents redaction collisions from hiding same-ID conflicts or terminal-run
mutations and detects later private unknown-field injection. Duplicate JSON
keys, NaN/Infinity, unsupported majors, broken run/Claim links, and corrupt
registry state also fail closed.

The content-free plan ID binds the strict input source-fingerprint set, the
entire current registry state, aggregate counts, and the final record set. It
changes even when a statement changes without changing counts, and it becomes
invalid after any registry-state change. A CLI plan mismatch performs no
registry write.

Fingerprints are integrity and privacy-regression detectors, not signatures or
authentication against a malicious writer with the same local user authority.
The registry remains a rebuildable derived index rather than a fact master; the
selected local contract files own their records.

Every imported Claim is stored with `trust_state=untrusted-import` and
`local_review_state=candidate`; a producer-supplied `accepted` value is retained
only as audit data. Candidate ingestion verifies declared cross-file identities,
not the physical artifact. A separate local human-promotion workflow must:

1. resolve the project in the local portfolio;
2. resolve the artifact inside the explicitly selected workspace;
3. recompute and match its SHA-256;
4. record the human decision before producing a reviewed Claim.

Until that promotion workflow completes, neither Context Capsule nor another
consumer may present the candidate as an authoritative project fact.

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
suite; Windows additionally exercises lock, reparse-point, research-artifact
ingestion, Context Capsule, pending/fill and public pull behavior.
