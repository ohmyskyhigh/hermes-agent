"""CLI for the owner/participants to record verified topic-campaign progress."""
import argparse
import json
from pathlib import Path
import time

from gateway.topic_campaign_store import CampaignStore


def configure(parser):
    parser.add_argument("--db", required=True, help="Shared campaign database configured on the gateway")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("list")
    for verb in ("show", "current", "history"):
        query = verbs.add_parser(verb)
        query.add_argument("key")
    update = verbs.add_parser("update")
    update.add_argument("key")
    for name in ("goal", "done_when", "progress", "next_action", "blocker", "evidence"):
        update.add_argument("--" + name.replace("_", "-"))
    update.add_argument("--status", choices=["running", "blocked", "completed"])
    amend = verbs.add_parser("amend", help="Record explicit human requirement changes while paused")
    amend.add_argument("key")
    amend.add_argument("--source-message", required=True)
    for name in ("goal", "done_when", "next_action"):
        amend.add_argument("--" + name.replace("_", "-"))
    work = verbs.add_parser("work")
    work.add_argument("key")
    work.add_argument("work_id")
    work.add_argument("--status", choices=["pending", "running", "blocked", "done", "cancelled"])
    work.add_argument("--summary")
    work.add_argument("--kind", choices=["request", "kanban"])
    work.add_argument("--task-id")
    work.add_argument("--board")
    work.add_argument("--assignee", dest="work_profile", help="Assigned worker profile (actor uses global --profile)")
    parser.set_defaults(func=command)


def build_parser(subparsers):
    parser = subparsers.add_parser("campaign", help="Inspect and update persistent topic campaigns")
    configure(parser)
    return parser


def build_work_parsers(subparsers, cmd_kanban):
    """Register persistent work commands together at the CLI boundary."""
    from hermes_cli.kanban import build_parser as build_kanban_parser
    build_kanban_parser(subparsers).set_defaults(func=cmd_kanban)
    build_parser(subparsers)


def execute(args, profile):
    if not Path(args.db).is_absolute() or not Path(args.db).is_file():
        raise ValueError("--db must name an existing absolute campaign database")
    store = CampaignStore(args.db)
    if args.verb == "list":
        return store.list()
    if args.verb == "history":
        return store.history(args.key)
    if args.verb in {"show", "current"}:
        item = store.get(args.key) if args.verb == "show" else store.current(args.key)
        if item is None:
            raise ValueError("campaign not found")
        return item
    if args.verb == "update":
        changes = {k: getattr(args, k) for k in ("goal", "done_when", "progress", "next_action", "blocker", "evidence", "status")
                   if getattr(args, k) is not None}
        return store.update(args.key, profile, changes, time.time())
    if args.verb == "amend":
        changes = {k: getattr(args, k) for k in ("goal", "done_when", "next_action") if getattr(args, k) is not None}
        return store.amend(args.key, profile, args.source_message, changes, time.time())
    changes = {k: getattr(args, k) for k in ("status", "summary", "kind", "task_id", "board")
               if getattr(args, k) is not None}
    if args.work_profile:
        changes["profile"] = args.work_profile
    return store.work_update(args.key, profile, args.work_id, changes, time.time())


def command(args):
    from hermes_cli.profiles import current_profile_name
    try:
        result = execute(args, current_profile_name("default"))
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    configure(parser)
    raise SystemExit(command(parser.parse_args()))
