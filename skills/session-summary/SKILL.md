---
name: session-summary
description: Produce and submit a reviewable summary-fill/1.0 object from one sanitized Provenance pending scaffold. Use when prov-sync-pull reports agent-fill, when a pending session needs an interactive summary, or when the user asks to close out a captured research session.
---

# Session summary

Turn one allowlisted pending scaffold into a factual `summary-fill/1.0` object.
Use the public pending/fill commands so the package retains path and identity
control.

## Workflow

1. List safe pending IDs:

   ```text
   prov-sync-pending --provenance-home %PROVENANCE_HOME% --json
   ```

2. Fetch exactly one scaffold by its opaque ID:

   ```text
   prov-sync-pending <summary-id> --provenance-home %PROVENANCE_HOME% --json
   ```

3. Stop if the scaffold is missing, unresolved, malformed, or not associated
   with a registered project. Use the project-resolution flow instead; never
   invent a project or session identity.
4. Build one JSON object:
   - Require `schema_version: "summary-fill/1.0"`.
   - Require `timeline` with 2-6 short, standalone factual bullets.
   - Include `status`, `stage`, `next_actions`, `conclusion`,
     `blocked_by`, or `confidence` only when the turns establish them.
5. Submit the reviewed object through stdin:

   ```text
   prov-sync-fill <summary-id> --provenance-home %PROVENANCE_HOME%
   ```

6. Run the pull preview again. Obtain separate authorization before `--run`.

## Output contract

```json
{
  "schema_version": "summary-fill/1.0",
  "timeline": ["Completed a bounded task.", "Verified the resulting behavior."],
  "status": "active",
  "stage": "review",
  "next_actions": ["Review the pending evidence."],
  "conclusion": "The bounded change is ready for review.",
  "blocked_by": "",
  "confidence": "medium"
}
```

Only `schema_version` and `timeline` are required.

## Guardrails

- Use only the sanitized scaffold returned by the public command. Do not seek
  the original transcript, reconstruct an absolute working directory, or read
  another session.
- Do not include `summary_id`, project identity, timestamps, or paths in the
  fill; the package binds those fields locally.
- Keep the work ephemeral and local. Do not copy session content into memory,
  logs, browser tools, telemetry, or network services.
- Match the session language while preserving exact JSON keys and enum values.
