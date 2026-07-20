---
name: provenance-profile
description: Build or refresh an evidence-grounded local profile from sanitized Provenance conversation digests. Use when the user asks to generate or update a persona card, profile report, project timeline, ideas list, or incremental profile from a Provenance data root.
---

# Provenance profile

Build a reviewable local profile from the user's sanitized conversation archive.
Treat the archive as sensitive input and keep every derived artifact local.

## Workflow

1. Confirm the data root.
   - Use `$PROVENANCE_HOME`; do not infer or search personal folders.
   - Stop if the sanitized `inbox/` is absent or empty.
2. Prepare deterministic inputs.
   - Run `prov-prep`.
   - Run `prov-organize` if `output/00_catalog.csv` is absent or stale.
   - Run `prov-diff` before updating an existing profile.
3. Read every `_prep/digest_*.md`. Open sanitized inbox records only when a
   digest lacks evidence needed for a claim.
4. Write these files under `profile/`:
   - `01_persona-card.md`: concise professional collaboration context.
   - `02_profile-report.md`: evidence-grounded strengths, preferences, and
     collaboration guidance.
   - `03_project-timeline.md`: cross-agent project threads and milestones.
   - `04_ideas-and-threads.md`: promising ideas that remain open.
5. Maintain `profile/_facts.json` as an incremental ledger. For each fact,
   record a stable ID, dimension, statement, confidence, first/last seen dates,
   status, and evidence references. Add, reinforce, revise, or mark dropped;
   never silently delete.
6. Report which local files changed. Do not commit profile outputs.

## Guardrails

- Use only sanitized local inputs. Make no network call, upload, telemetry event,
  or external-memory write.
- Ground every statement in cited conversation evidence. Mark uncertainty and
  omit unsupported claims.
- Keep professional material separate from sensitive personal analysis.
- De-identify outputs: use roles and broad categories instead of names,
  institutions, private repositories, financial details, or precise locations.
- Match the user's working language; keep commands and data keys unchanged.
