"""Public on-demand pull orchestration for the Provenance sync layer.

The command is a deterministic local control surface. It previews by default and
only writes with ``--run``. It never calls a model, approves unchecked claims, or
installs host hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from provenance import __version__
from provenance.ingest import notes as note_ingest
from provenance.ingest import vault as vault_ingest
from provenance.synclayer import approvals, paths, scan_agents, summarize_driver, worker


FORMAT_VERSION = 1
GENERATED_BY = {"name": "provenance", "version": __version__}
CAPABILITIES = {
    "dry_run_default": True,
    "explicit_run": True,
    "structured_report": True,
    "pull_lock": True,
    "codex_scan": True,
    "applies_checked_approvals": True,
    "model_calls": False,
    "installs_hooks": False,
    "network_requests": False,
}


def _egress_report():
    return {
        "suite_managed": "not-requested",
        "host_managed": "not-invoked",
        "optional_connectors": "not-invoked",
    }


class PullUsageError(ValueError):
    """The command lacks a safe, explicit data root or workspace."""


def capabilities_report():
    return {
        "format_version": FORMAT_VERSION,
        "operation": "pull.capabilities",
        "generated_by": dict(GENERATED_BY),
        "status": "ok",
        "exit_code": 0,
        "action_required": [],
        "egress": _egress_report(),
        "capabilities": dict(CAPABILITIES),
    }


def _new_report(run):
    return {
        "format_version": FORMAT_VERSION,
        "operation": "pull",
        "generated_by": dict(GENERATED_BY),
        "mode": "run" if run else "preview",
        "status": "ok",
        "exit_code": 0,
        "egress": _egress_report(),
        "stages": [],
        "summary": {
            "projects": 0,
            "notes_ingested": 0,
            "notes_updated": 0,
            "notes_unchanged": 0,
            "notes_planned": 0,
            "notes_unstable": 0,
            "codex_found": 0,
            "codex_enqueued": 0,
            "worker_events": 0,
            "planned_scaffolds": 0,
            "planned_applies": 0,
            "scaffolded": 0,
            "applied": 0,
            "draft_only": 0,
            "unresolved_events": 0,
            "pending_fill": 0,
            "checked_approvals": 0,
            "approved": 0,
            "pending_approval": 0,
        },
        "action_required": [],
        "errors": [],
        "limitations": [
            "Pending summary fills require an in-session agent review.",
            "Unresolved events stay inflight until a registered project mapping exists.",
            "Only approvals already checked by the user are applied.",
            "The project filter narrows Codex discovery only; other stages remain workspace-wide.",
        ],
    }


def _add_stage(report, stage_id, status, counts=None):
    stage = {"id": stage_id, "status": status}
    if counts is not None:
        stage["counts"] = counts
    report["stages"].append(stage)


def _mark(report, status, exit_code, code, message):
    if exit_code > report["exit_code"]:
        report["status"] = status
        report["exit_code"] = exit_code
    report["errors"].append({"code": code, "message": message})


def _finish(report):
    summary = report["summary"]
    if not report["exit_code"] and report["mode"] == "preview":
        report["action_required"].append({"type": "run-confirmation"})
    if summary["pending_fill"]:
        report["action_required"].append(
            {"type": "agent-fill", "count": summary["pending_fill"]}
        )
    if summary["unresolved_events"]:
        report["action_required"].append(
            {"type": "project-resolution", "count": summary["unresolved_events"]}
        )
    if summary["pending_approval"]:
        report["action_required"].append(
            {"type": "human-approval", "count": summary["pending_approval"]}
        )
    workspace_review = summary["notes_unstable"] + summary["draft_only"]
    if workspace_review:
        report["action_required"].append(
            {"type": "workspace-review", "count": workspace_review}
        )
    if report["exit_code"]:
        return report
    if report["mode"] == "preview":
        report["status"] = "planned" if report["action_required"] else "noop"
    else:
        report["status"] = (
            "action-required" if report["action_required"] else "completed"
        )
    return report


def _required_path(explicit, env_name, label):
    value = explicit if explicit is not None else os.environ.get(env_name)
    if value is None or not str(value).strip():
        raise PullUsageError(f"{label} is required")
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PullUsageError(f"{label} is invalid") from exc
    if not path.is_dir():
        raise PullUsageError(f"{label} must be an existing directory")
    return path


def _validate_sync_jsonl(root):
    state = paths.sync_state(root)
    candidates = [paths.queue_file(root), paths.inflight_file(root)]
    if state.exists():
        candidates.extend(sorted(state.glob("queue.claim.*.jsonl")))

    files = records = invalid = 0
    for candidate in candidates:
        if not candidate.exists():
            continue
        files += 1
        try:
            lines = candidate.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            invalid += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                invalid += 1
                continue
            if not isinstance(value, dict):
                invalid += 1
                continue
            records += 1
    return {"files": files, "records": records, "invalid": invalid}


def _safe_stage_failure(report, stage_id):
    _add_stage(report, stage_id, "partial")
    _mark(
        report,
        "partial",
        1,
        "stage_failed",
        "A pull stage could not complete safely.",
    )


def _execute(report, workspace, root, *, run, scan_codex, codex_home, project):
    validation = _validate_sync_jsonl(root)
    if validation["invalid"]:
        _add_stage(report, "sync_state", "blocked", validation)
        _mark(
            report,
            "blocked",
            1,
            "invalid_sync_jsonl",
            "Sync queue state is malformed; no worker action was attempted.",
        )
        return report
    _add_stage(report, "sync_state", "ok", validation)

    try:
        portfolio = vault_ingest.ingest(workspace, root=root, run=run)
    except (OSError, UnicodeError, ValueError):
        _safe_stage_failure(report, "portfolio")
        return report
    except Exception:
        _add_stage(report, "portfolio", "error")
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report
    projects = portfolio["projects"]
    report["summary"]["projects"] = portfolio["count"]
    _add_stage(
        report,
        "portfolio",
        "ok" if run else "planned",
        {"projects": portfolio["count"]},
    )

    try:
        note_results = note_ingest.ingest(
            workspace,
            note_ingest.DEFAULT_ZONES,
            root=root,
            run=run,
        )
    except (OSError, UnicodeError, ValueError):
        _safe_stage_failure(report, "notes")
        return report
    except Exception:
        _add_stage(report, "notes", "error")
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report
    note_counts = Counter(item.get("state") for item in note_results)
    note_stage_counts = {
        "ingested": note_counts["ingested"],
        "updated": note_counts["updated"],
        "unchanged": note_counts["skipped"],
        "planned": note_counts["would_ingest"] + note_counts["would_update"],
        "unstable": note_counts["unstable"],
        "errors": note_counts["error"],
    }
    report["summary"]["notes_ingested"] = note_counts["ingested"]
    report["summary"]["notes_updated"] = note_counts["updated"]
    report["summary"]["notes_unchanged"] = note_counts["skipped"]
    report["summary"]["notes_planned"] = note_stage_counts["planned"]
    report["summary"]["notes_unstable"] = note_counts["unstable"]
    note_status = "partial" if note_counts["error"] else (
        "action-required" if note_counts["unstable"] else "ok"
    )
    _add_stage(report, "notes", note_status, note_stage_counts)
    if note_counts["error"]:
        _mark(
            report,
            "partial",
            1,
            "note_ingest_failed",
            "One or more private notes could not be ingested.",
        )

    if scan_codex:
        try:
            preview = scan_agents.scan_codex(
                root=root,
                codex_home=codex_home,
                run=False,
                project=project,
                projects=projects,
            )
            scanned = preview
            if run and preview["found"]:
                scanned = scan_agents.scan_codex(
                    root=root,
                    codex_home=codex_home,
                    run=True,
                    project=project,
                    projects=projects,
                )
        except (OSError, UnicodeError, ValueError):
            _safe_stage_failure(report, "codex_scan")
            return report
        except Exception:
            _add_stage(report, "codex_scan", "error")
            _mark(report, "error", 2, "internal_error", "Pull failed internally.")
            return report
        report["summary"]["codex_found"] = preview["found"]
        report["summary"]["codex_enqueued"] = scanned["enqueued"]
        _add_stage(
            report,
            "codex_scan",
            "ok",
            {"found": preview["found"], "enqueued": scanned["enqueued"]},
        )
    else:
        _add_stage(report, "codex_scan", "skipped", {"found": 0, "enqueued": 0})

    try:
        worker_results = worker.drain(
            root=root,
            run=run,
            vault=workspace,
            projects=projects,
        )
    except worker.WorkerBusy:
        _add_stage(report, "worker", "blocked")
        _mark(report, "blocked", 1, "worker_busy", "The sync worker is busy.")
        return report
    except worker.WorkerStateError:
        _add_stage(report, "worker", "blocked")
        _mark(
            report,
            "blocked",
            1,
            "worker_state_failed",
            "Worker state could not be changed safely; inspect sync-state before retrying.",
        )
        return report
    except (OSError, UnicodeError, ValueError):
        _safe_stage_failure(report, "worker")
        return report
    except Exception:
        _add_stage(report, "worker", "error")
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report

    worker_counts = Counter(item.get("state") for item in worker_results)
    draft_only = sum(1 for item in worker_results if item.get("routed") == "inbox-draft")
    worker_stage_counts = {
        "events": len(worker_results),
        "would_scaffold": worker_counts["would_scaffold"],
        "would_apply": worker_counts["would_apply"],
        "scaffolded": worker_counts["scaffolded"],
        "applied": worker_counts["applied"],
        "no_transcript": worker_counts["no_transcript"],
        "unresolved_project": worker_counts["unresolved_project"],
        "errors": worker_counts["error"],
        "draft_only": draft_only,
    }
    report["summary"]["worker_events"] = len(worker_results)
    report["summary"]["planned_scaffolds"] = worker_counts["would_scaffold"]
    report["summary"]["planned_applies"] = worker_counts["would_apply"]
    report["summary"]["scaffolded"] = worker_counts["scaffolded"]
    report["summary"]["applied"] = worker_counts["applied"]
    report["summary"]["draft_only"] = draft_only
    report["summary"]["unresolved_events"] = worker_counts["unresolved_project"]
    worker_failed = worker_counts["error"] or worker_counts["no_transcript"]
    worker_status = "partial" if worker_failed else (
        "action-required" if worker_counts["unresolved_project"] else "ok"
    )
    _add_stage(report, "worker", worker_status, worker_stage_counts)
    if worker_failed:
        _mark(
            report,
            "partial",
            1,
            "worker_event_failed",
            "One or more queued events could not be advanced.",
        )

    pending_dirs = summarize_driver.find_pending(root)
    pending_ids = {item.name for item in pending_dirs}
    unresolved_ids = {
        item.get("summary_id")
        for item in worker_results
        if item.get("state") == "unresolved_project" and item.get("summary_id")
    }
    if not run:
        pending_ids.update(
            item.get("summary_id")
            for item in worker_results
            if item.get("state") == "would_scaffold" and item.get("summary_id")
        )
    pending_ids.difference_update(unresolved_ids)
    report["summary"]["pending_fill"] = len(pending_ids)
    _add_stage(report, "pending", "ok", {"fills": len(pending_ids)})

    try:
        approve_plan = approvals.approve(workspace, root=root, run=run)
    except (OSError, UnicodeError, ValueError):
        _safe_stage_failure(report, "approvals_apply")
        return report
    except Exception:
        _add_stage(report, "approvals_apply", "error")
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report
    if approve_plan["state"] == "refused_not_generated":
        _add_stage(report, "approvals_apply", "blocked", {"checked": 0, "applied": 0})
        _mark(
            report,
            "blocked",
            1,
            "approvals_not_owned",
            "The approvals file is not owned by Provenance and was left untouched.",
        )
        return report
    checked = approve_plan.get("count", 0)
    report["summary"]["checked_approvals"] = checked
    report["summary"]["approved"] = checked if run else 0
    _add_stage(
        report,
        "approvals_apply",
        "ok" if run else "planned",
        {"checked": checked, "applied": checked if run else 0},
    )

    try:
        refresh = approvals.regenerate(workspace, root=root, run=run)
    except (OSError, UnicodeError, ValueError):
        _safe_stage_failure(report, "approvals_refresh")
        return report
    except Exception:
        _add_stage(report, "approvals_refresh", "error")
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report
    if refresh["state"] == "refused_not_generated":
        _add_stage(report, "approvals_refresh", "blocked", {"pending": 0})
        _mark(
            report,
            "blocked",
            1,
            "approvals_not_owned",
            "The approvals file is not owned by Provenance and was left untouched.",
        )
        return report
    report["summary"]["pending_approval"] = refresh["pending"]
    _add_stage(
        report,
        "approvals_refresh",
        "ok" if run else "planned",
        {"pending": refresh["pending"]},
    )

    # The portfolio snapshot is ingested before the worker so session resolution
    # uses a stable view. When this run writes a progress block or applies an
    # approval, refresh that snapshot before returning so an immediate read-only
    # context capsule observes the just-completed writeback.
    if run and (worker_counts["applied"] or checked):
        try:
            refreshed_portfolio = vault_ingest.ingest(
                workspace, root=root, run=True
            )
        except (OSError, UnicodeError, ValueError):
            for stage in report["stages"]:
                if stage.get("id") == "portfolio":
                    stage["status"] = "partial"
                    break
            _mark(
                report,
                "partial",
                1,
                "stage_failed",
                "The project writeback completed but its memory snapshot could not be refreshed.",
            )
            return report
        except Exception:
            for stage in report["stages"]:
                if stage.get("id") == "portfolio":
                    stage["status"] = "error"
                    break
            _mark(report, "error", 2, "internal_error", "Pull failed internally.")
            return report
        report["summary"]["projects"] = refreshed_portfolio["count"]
    return _finish(report)


def run_pull(
    *,
    workspace=None,
    provenance_home=None,
    run=False,
    scan_codex=False,
    codex_home=None,
    project=None,
):
    report = _new_report(run)
    try:
        workspace_path = _required_path(workspace, "PROVENANCE_VAULT", "workspace")
        root = _required_path(
            provenance_home, "PROVENANCE_HOME", "provenance home"
        )
    except PullUsageError:
        _mark(
            report,
            "error",
            2,
            "invalid_configuration",
            "An explicit workspace and Provenance home are required.",
        )
        return report

    if root == workspace_path or workspace_path in root.parents:
        _mark(
            report,
            "error",
            2,
            "unsafe_data_root",
            "Provenance home must be outside the workspace.",
        )
        return report

    if not run:
        _add_stage(report, "pull_lock", "skipped")
        try:
            return _execute(
                report,
                workspace_path,
                root,
                run=False,
                scan_codex=scan_codex,
                codex_home=codex_home,
                project=project,
            )
        except Exception:
            _mark(report, "error", 2, "internal_error", "Pull failed internally.")
            return report

    try:
        with worker._Lock(paths.pull_lock_file(root)):
            _add_stage(report, "pull_lock", "ok")
            return _execute(
                report,
                workspace_path,
                root,
                run=True,
                scan_codex=scan_codex,
                codex_home=codex_home,
                project=project,
            )
    except worker.WorkerBusy:
        _add_stage(report, "pull_lock", "blocked")
        _mark(
            report,
            "blocked",
            1,
            "pull_busy",
            "Another pull is active, or a legacy lock requires review.",
        )
        return report
    except worker.LockReleaseError:
        for stage in report["stages"]:
            if stage.get("id") == "pull_lock":
                stage["status"] = "blocked"
                break
        _mark(
            report,
            "blocked",
            1,
            "lock_release_failed",
            "The pull lock could not be released; inspect sync-state before retrying.",
        )
        return report
    except worker.WorkerStateError:
        _add_stage(report, "pull_lock", "blocked")
        _mark(
            report,
            "blocked",
            1,
            "worker_state_failed",
            "Pull coordination state could not be changed safely.",
        )
        return report
    except Exception:
        _mark(report, "error", 2, "internal_error", "Pull failed internally.")
        return report


def build_parser():
    parser = argparse.ArgumentParser(
        prog="prov-sync-pull",
        description="Preview or run the local on-demand Provenance sync pipeline.",
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--provenance-home", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--capabilities", action="store_true")
    parser.add_argument("--scan-codex", action="store_true")
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="actual CODEX_HOME; sessions are read from <codex-home>/sessions",
    )
    parser.add_argument(
        "--project",
        help="limit Codex discovery only; all other pull stages remain workspace-wide",
    )
    return parser


def _format_report(report):
    if report["operation"] == "pull.capabilities":
        enabled = sum(1 for value in report["capabilities"].values() if value)
        return f"Provenance pull capabilities: {enabled}/{len(report['capabilities'])} enabled"
    summary = report["summary"]
    return (
        f"Provenance pull {report['mode']}: {report['status']}\n"
        f"projects={summary['projects']} notes_planned={summary['notes_planned']} "
        f"scaffolded={summary['scaffolded']} applied={summary['applied']} "
        f"unresolved_events={summary['unresolved_events']} "
        f"pending_fill={summary['pending_fill']} "
        f"pending_approval={summary['pending_approval']} approved={summary['approved']}"
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = capabilities_report() if args.capabilities else run_pull(
        workspace=args.workspace,
        provenance_home=args.provenance_home,
        run=args.run,
        scan_codex=args.scan_codex,
        codex_home=args.codex_home,
        project=args.project,
    )
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
