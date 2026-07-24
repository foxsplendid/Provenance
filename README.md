English | [中文](README.zh.md)

# Provenance

> A local-first memory and evidence bridge for AI-assisted research.

Provenance turns local AI-session records, Markdown project notes, literature
metadata, and reviewable agent summaries into a searchable, recoverable research
context. It owns the local memory state; Claude Code, Codex, Cursor, and other
agents are clients of that state rather than its source of truth.

Provenance is an independent product in the Scriptorium suite. It consumes shared
file contracts, but it does not call another suite component's internals.

## Public release boundary

This repository contains only code, documentation, neutral Skills, and synthetic
test fixtures. It contains no personal conversation archive, research corpus,
profile, project state, local path, or private configuration.

The optional clean-room browser exporter lives in [`01-capture/`](01-capture/).
It runs only after an explicit click, makes same-origin provider requests, and
creates a local ZIP without third-party JavaScript, telemetry, extension storage,
or background work. Its provider endpoints are undocumented and may change; read
its [README](01-capture/README.md) and [privacy boundary](01-capture/PRIVACY.md)
before use. The Python package remains separate and never discovers or launches
the extension.

## What it does

- Imports supported local coding-agent logs, compatible conversation ZIPs,
  Google Takeout activity, Markdown notes, a project portfolio, and a literature
  library snapshot.
- Provides an optional, manually triggered browser producer for compatible
  ChatGPT and Claude conversation ZIPs.
- Scrubs common credential and PII shapes, then applies an optional local alias
  map before records enter the working layer.
- Organizes conversation records, builds deterministic preparation digests,
  exports a Markdown/Obsidian view, and creates a local SQLite FTS5 index.
- Exposes read-only research context through a stdlib JSON-RPC/MCP server.
- Ingests validated `parsed-paper/1.0`, `reading-note/1.0`, `review/1.0`, and
  `lineage-graph/1.0` artifacts through an atomic, lock-protected local merge.
- Builds a bounded Context Capsule that separates approved project state,
  auto-applied low-risk progress, and reference-only literature leads.
- Captures session-closeout events into a local queue and advances them through a
  resumable, lock-protected state machine.
- Requires a resolved registered project before a scaffold or
  `session-summary/1.0` can exist.
- Keeps high-value claims as drafts until a human approves them.
- Ships neutral `skills/` for profile synthesis and interactive session summary
  filling; the Python package itself does not call a model.

## Product architecture

```text
Local sources
  optional browser export | agent logs | compatible ZIP | Takeout | Markdown
  library snapshot
                               |
                               v
Ingest + privacy boundary
  normalize -> secret scrub -> optional pseudonymization
                               |
                               v
Local research memory
  inbox | memory | sync-state | SQLite FTS5
            |          |
            |          +-> pending scaffold -> reviewed fill -> timeline/draft
            |                                             -> human approval
            v
Local consumers
  CLI | Obsidian/Markdown | read-only MCP | agent Skills
```

All runtime data lives under `PROVENANCE_HOME`. Keep that directory outside the
source checkout and out of cloud-synced folders unless you have independently
reviewed their privacy controls.

## Requirements

- Windows 10/11 is the first supported release target.
- Python 3.9 or newer.
- `uv` 0.11.16 for the locked development environment.
- A local coding agent when using the two agent Skills.
- SQLite 3.34 or newer for trigram FTS5; older builds fail over with a warning.

The installed Python package has no third-party runtime dependency and contains
no network client path. A host agent remains subject to that host's own model,
network, and retention settings.

## Install on Windows

```powershell
git clone https://github.com/foxsplendid/Provenance.git
Set-Location Provenance
uv sync --locked

$env:PROVENANCE_HOME = "C:\ScriptoriumDemo\provenance-data"
New-Item -ItemType Directory -Force $env:PROVENANCE_HOME | Out-Null
uv run --locked prov-status
```

Persist `PROVENANCE_HOME` only after reviewing the selected directory. Never use
the repository directory as a long-term private data root.

## Core workflows

### Import and refresh

```powershell
# Supported local coding-agent logs
uv run --locked prov-ingest-agents

# Google Takeout ZIP selected by the user
uv run --locked prov-ingest-takeout C:\ScriptoriumDemo\imports\Takeout-demo.zip

# Existing compatible provenance__*.zip archives
uv run --locked prov-ingest C:\ScriptoriumDemo\imports

# Rebuild local organization, search, and exports
uv run --locked prov-sync
uv run --locked prov-status
```

All examples above use synthetic paths. Do not attach a real archive, generated
profile, `sync-state` report, or local data-root listing to a public issue.

### Optional browser export

The source under `01-capture/` can be loaded unpacked in Chrome or Edge. It is a
separate, user-triggered producer; it does not change the Python package's local,
no-network runtime boundary.

```powershell
Set-Location 01-capture
npm test
python -B pack.py
```

Load `01-capture/` as an unpacked extension for development, or review the
deterministic archive under its ignored `dist/` directory. Provider internal APIs
are unofficial and can break independently of Provenance ingestion.

### Literature and project context

```powershell
uv run --locked prov-ingest-library
uv run --locked prov-ingest-research C:\ScriptoriumDemo\steward-output
uv run --locked prov-ingest-vault C:\ScriptoriumDemo\workspace
uv run --locked prov-context --project synthetic-catalyst --json
uv run --locked prov-search "research gap"
```

The default literature input is
`%PROVENANCE_HOME%\kb\library.json`. The project portfolio comes from the
workspace's `Projects/` frontmatter. Obsidian is optional; any Markdown workspace
that follows the same contract can be used. Research-artifact inputs must be
explicit files/directories; the command does not discover personal folders or
persist their source paths. It rejects symbolic links and Windows reparse points
in every input or output path component, verifies file identity around reads and
writes, and redacts absolute local paths including
`file:` URIs while keeping read/write failures path-free. Capsule literature hints remain reference-only until
the researcher reviews evidence and approves a claim.

### Read-only MCP

```powershell
uv run --locked prov-mcp
```

The server exposes `search_brain`, `get_profile`, `list_topics`, `get_portfolio`,
`get_current_context`, and `get_context_capsule` over stdio. It performs no
network request and does not write project state.

### Session writeback

Preview first:

```powershell
uv run --locked prov-sync-pull `
  --provenance-home $env:PROVENANCE_HOME `
  --workspace C:\ScriptoriumDemo\workspace `
  --json
```

Run one reviewed local pass only after explicit authorization:

```powershell
uv run --locked prov-sync-pull `
  --provenance-home $env:PROVENANCE_HOME `
  --workspace C:\ScriptoriumDemo\workspace `
  --run --json
```

Pending summaries use opaque IDs:

```powershell
uv run --locked prov-sync-pending --provenance-home $env:PROVENANCE_HOME --json
uv run --locked prov-sync-pending <summary-id> --provenance-home $env:PROVENANCE_HOME --json
uv run --locked prov-sync-fill <summary-id> --provenance-home $env:PROVENANCE_HOME
```

An unresolved project stays in protected inflight state. It produces no scaffold,
timeline, draft, or `session-summary/1.0` until the user approves a project
mapping. See [the wiring runbook](docs/sync-layer-wiring.md).

## Commands

| Command | Purpose |
|---|---|
| `prov-sync` | Run the local refresh pipeline |
| `prov-status` | Report local archive status |
| `prov-watch` | Watch a user-selected drop folder |
| `prov-ingest` | Import compatible `provenance__*.zip` archives |
| `prov-ingest-takeout` | Import a selected Google Takeout ZIP |
| `prov-ingest-agents` | Import supported local coding-agent logs |
| `prov-ingest-library` | Import a `library-kb/1.x` snapshot |
| `prov-ingest-research` | Atomically ingest explicit literature-reading artifacts |
| `prov-ingest-vault` | Import project frontmatter |
| `prov-ingest-notes` | Import private Markdown zones into protected state |
| `prov-scrub` | Re-apply the current privacy transform |
| `prov-organize` / `prov-prep` / `prov-diff` | Build deterministic derived views |
| `prov-obsidian` | Export a local Markdown/Obsidian view |
| `prov-search` | Build/query the local FTS5 index |
| `prov-mcp` | Start the read-only stdio MCP server |
| `prov-context` | Print one bounded, read-only project Context Capsule |
| `prov-sync-pull` | Preview or run one lock-protected sync pass |
| `prov-sync-pending` / `prov-sync-fill` | Review and submit one safe summary fill |
| `prov-sync-unresolved` | Inspect unresolved mappings without mutation |
| `prov-sync-approve` | Generate and apply the human approval queue |

Every command supports `--help`.

## Skills

- `skills/provenance-profile`: builds de-identified, evidence-grounded local
  profile artifacts from sanitized digests.
- `skills/session-summary`: turns exactly one allowlisted scaffold into a
  reviewable `summary-fill/1.0` object and submits it through public commands.

Both Skills prohibit network use and direct access to raw transcripts or another
session. Their outputs remain local and must not be committed.

## Repository layout

```text
provenance/    installable stdlib Python package
01-capture/    optional clean-room browser exporter
skills/        neutral agent Skills
harden/        opt-in Windows wiring templates
docs/          architecture, privacy, and sync-layer contracts
tests/         synthetic unit and end-to-end fixtures
tools/         public-tree privacy gate
```

Runtime directories such as `inbox/`, `drop/`, `memory/`, `profile/`, `vault/`,
`sync-state/`, and `search-index.db` are ignored and must remain private.

## Verify

```powershell
uv run --locked python -m unittest discover -s tests -v
uv run --locked python tools/check_public_tree.py
python -B 01-capture\tests\test_extension.py
python C:\path\to\skill-creator\scripts\quick_validate.py skills\provenance-profile
python C:\path\to\skill-creator\scripts\quick_validate.py skills\session-summary
```

CI runs the Python and extension tests on Linux and Windows. It also checks the
public tree for forbidden private artifacts and non-example identities.

## Privacy and security

Sanitization reduces accidental disclosure; it does not make research data safe
to publish. Attachments remain original bytes. The alias map is a re-identification
key. Queue, draft, journal, profile, and memory outputs can all remain sensitive.
Read [docs/PRIVACY.md](docs/PRIVACY.md) before importing real data.

For a suspected vulnerability, do not open a public issue containing data or a
reproduction archive. First reduce it to a synthetic fixture.

## Status

The local Python pipeline, MCP surface, event/sync safety contracts, and optional
browser exporter source are implemented and tested. The extension is currently a
developer-mode workflow, not a browser-store submission or a provider-stability
guarantee.

## License

[MIT](LICENSE). See [NOTICE](NOTICE) for the exact public release scope.
