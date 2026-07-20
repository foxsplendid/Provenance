# Human approval surface

The approval layer separates a useful Agent proposal from accepted project state.
A session timeline can be appended through the bounded progress-log primitive;
status, stage, next actions, conclusions and blockers remain drafts until the user
reviews them.

## Flow

```text
sync-state/drafts/<id>.json
        |
        | prov-sync-approve generate
        v
workspace/Approvals.md
        |
        | user edits and ticks selected fields
        v
prov-sync-approve approve --run
        |
        +-> append approved claims to the managed progress log
        +-> update memory/applied_claims.json overlay
        +-> mark the draft applied with audit metadata
```

Both verbs preview by default. `--run` is required for a write.

## Queue contract

`Approvals.md` is a generated but user-editable Markdown view:

- a fixed marker identifies the managed file;
- the generator refuses to overwrite a same-named unmarked file;
- each draft has a stable marker and plain checkbox;
- regeneration preserves user edits and checkbox state;
- new drafts are appended unchecked;
- applied or withdrawn drafts disappear from the regenerated queue;
- unresolved, timeline-only and invalid drafts are excluded.

The queue must not contain raw transcript text, absolute paths or session IDs.

## Commit contract

For each checked draft:

1. Parse the values from the reviewed queue, not blindly from the original Agent
   proposal.
2. Append approved claims inside the project note's managed marker region using a
   distinct idempotency key.
3. Update `memory/applied_claims.json`, which overlays the imported project
   snapshot and survives a later vault re-ingest.
4. Mark the source draft `applied` with the exact committed values.
5. Regenerate the queue so applied items disappear.

Hand-authored content outside the managed progress-log region is byte-preserved.
When the project note or marker is unavailable, the operation must report the
limited result rather than silently growing an arbitrary Markdown file.

## Safety boundaries

- No notification or mobile-drop source may approve a claim.
- A malformed draft, project mismatch or forged identity blocks the commit.
- Empty optional fields never erase an existing user-authored value.
- Journal records make file mutations reversible.
- Repeating the same approval is a no-op.
- `Approvals.md`, drafts and applied-claim overlays are private runtime data and
  must never be committed or uploaded.

## Verification

`tests/test_synclayer_m3.py` covers generation, merge-preserving regeneration,
edit-before-approve, frontmatter preservation, overlay survival, journaling,
rollback, invalid inputs, dry-run behavior and idempotency using only synthetic
projects.

