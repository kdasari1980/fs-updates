"""Command-line interface for `fs`."""
import argparse
import json
import sys

from . import checks, creates, reads, removes, writes
from .cache import MetaCache
from .client import Client, FSError, load_api_key

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
# Apply refuses to start a batch the account's remaining API calls can't cover (and still leave this much).
APPLY_RESERVE = 50


def _split(csv):
    return [x.strip() for x in csv.split(",") if x.strip()]


def _emit(obj):
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def _err(msg):
    print(f"error: {msg}", file=sys.stderr)


def _fmt(v):
    if v is None or v == "":
        return "(empty)"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "(empty)"
    return str(v)


def _table(headers, rows):
    rows = [[_fmt(c) if not isinstance(c, str) else c for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths))  # noqa: E731
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _print_diff(plan):
    ident = "(new)" if plan["id"] is None else plan["id"]
    print(f"\n== {plan['op']} {plan['entity']} {ident} ({plan.get('label') or '?'}) ==",
          file=sys.stderr)
    rows = []
    for ch in plan["changes"]:
        flag = "OVERWRITE" if ch["overwrites_existing"] else ("change" if ch["changed"] else "same")
        if ch.get("source") == "default":
            flag = "default"
        flag = ch.get("flag", flag)
        rows.append([ch["field"], _fmt(ch["before"]), _fmt(ch["after"]), flag])
    if rows:
        print(_table(["field", "before", "after", ""], rows), file=sys.stderr)
    for w in plan.get("warnings", []):
        print(f"  WARNING: {w}", file=sys.stderr)
    for e in plan["errors"]:
        print(f"  INVALID: {e}", file=sys.stderr)


def _print_verification(plan, result):
    rows = [[r["field"], _fmt(r["intended"]), _fmt(r["actual"]), "ok" if r["ok"] else "DID NOT TAKE"]
            for r in result["verification"]]
    ident = result.get("created_id", plan["id"])
    print(f"\n-- verification {plan['entity']} {ident}: {result['status']} --", file=sys.stderr)
    if result.get("url"):
        print(f"   {result['url']}", file=sys.stderr)
    if result.get("note"):
        print(f"   {result['note']}", file=sys.stderr)
    if rows:
        print(_table(["field", "intended", "actual", ""], rows), file=sys.stderr)


# ---------- write command plumbing ----------

def _load_batch(path, op):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FSError(f"cannot read batch file {path}: {exc}") from None
    if isinstance(data, dict):
        data = data.get("operations", [data])
    if not isinstance(data, list):
        raise FSError("batch file must be a JSON list of operations")
    ops = []
    is_create = op.startswith("create-")
    for i, item in enumerate(data):
        if not isinstance(item, dict) or (not is_create and "id" not in item):
            raise FSError(f"batch item {i}: must be an object with an 'id'")
        if is_create and not isinstance(item.get("set", {}), dict):
            raise FSError(f"batch item {i}: 'set' must be an object")
        if item.get("op", op) != op:
            raise FSError(f"batch item {i}: op {item.get('op')!r} does not match command {op!r}")
        ops.append(item)
    return ops


def _collect_ops(args, op):
    if op.startswith("create-"):
        if args.batch:
            if args.set or args.preset:
                raise FSError("give either --set/--preset or --batch, not both")
            return _load_batch(args.batch, op)
        return [{"set": writes.parse_sets(args.set), "preset": args.preset}]
    if op.startswith("delete-"):
        ids = [i for raw in args.ids for i in _split(raw)]
        if not ids:
            raise FSError(f"{op} needs one or more ids")
        return [{"id": i} for i in ids]
    if op == "merge-contacts":
        return [{"id": args.id, "with": args.with_ids}]
    if args.batch:
        if getattr(args, "id", None):
            raise FSError("give either an id or --batch, not both")
        return _load_batch(args.batch, op)
    if not args.id:
        raise FSError(f"{op} needs an id (or --batch FILE)")
    if op == "add-note":
        if args.text is None:
            raise FSError("add-note needs the note text")
        return [{"id": args.id, "text": args.text, "on": args.on}]
    if op == "link-deal-contacts":
        return [{"id": args.id, "add": args.add, "remove": args.remove}]
    return [{"id": args.id, "set": writes.parse_sets(args.set)}]


def _ids_arg(v):
    return ",".join(str(x) for x in v) if isinstance(v, list) else v


def _plan(ctx, op, item):
    if op.startswith("create-"):
        sets = {k: str(v) for k, v in (item.get("set") or {}).items()}
        return creates.plan_create(ctx, op.split("-")[1], sets, item.get("preset"))
    if op.startswith("delete-"):
        return removes.plan_delete(ctx, op.split("-")[1], item["id"])
    if op == "merge-contacts":
        return removes.plan_merge_contacts(ctx, item["id"], _ids_arg(item.get("with")))
    if op == "update-contact":
        sets = item.get("set")
        if not isinstance(sets, dict):
            raise FSError(f"{op} {item['id']}: 'set' must be an object")
        return writes.plan_update_contact(ctx, item["id"], {k: str(v) for k, v in sets.items()})
    if op == "update-account":
        sets = item.get("set")
        if not isinstance(sets, dict):
            raise FSError(f"{op} {item['id']}: 'set' must be an object")
        return writes.plan_update_account(ctx, item["id"], {k: str(v) for k, v in sets.items()})
    if op == "update-deal":
        sets = item.get("set")
        if not isinstance(sets, dict):
            raise FSError(f"{op} {item['id']}: 'set' must be an object")
        return writes.plan_update_deal(ctx, item["id"], {k: str(v) for k, v in sets.items()})
    if op == "link-deal-contacts":
        return writes.plan_link_deal_contacts(ctx, item["id"], _ids_arg(item.get("add")), _ids_arg(item.get("remove")))
    return writes.plan_add_note(ctx, item["id"], item.get("text"), item.get("on") or "contact")


def _apply(ctx, plan):
    op = plan["op"]
    if op.startswith("create-"):
        return creates.apply_create(ctx, plan)
    if op.startswith("delete-"):
        return removes.apply_delete(ctx, plan)
    if op == "merge-contacts":
        return removes.apply_merge(ctx, plan)
    return writes.apply_plan(ctx, plan)


def run_write(client, args, op):
    ops = _collect_ops(args, op)
    ctx = writes.Context(client)
    if op == "create-contact":
        # one search for every email in the batch instead of one lookup per contact
        emails = [e for item in ops for k in ("email", "emails") for e in
                  reads_split(str((item.get("set") or {}).get(k, "")))]
        creates.prefetch_duplicates(ctx, emails)

    plans, failed = [], False
    for item in ops:
        try:
            plan = _plan(ctx, op, item)
        except FSError as exc:
            plan = {"op": op, "entity": op.split("-")[1], "id": item.get("id"), "label": None,
                    "changes": [], "errors": [str(exc)], "warnings": [], "request": None, "_checks": []}
        plans.append(plan)
        _print_diff(plan)
        failed = failed or bool(plan["errors"])

    if not args.apply:
        print("\nDRY RUN: no changes sent. Re-run with --apply to write.", file=sys.stderr)
        _emit({"mode": "dry-run", "operations": [writes.public(p) for p in plans]})
        return EXIT_FAIL if failed else EXIT_OK

    if failed:
        _err("validation failed; nothing was sent (whole batch aborted)")
        _emit({"mode": "apply", "sent": False, "operations": [writes.public(p) for p in plans]})
        return EXIT_FAIL

    needed = sum(1 for p in plans if p["request"])
    left = client.remaining
    if left is not None and needed > max(left - APPLY_RESERVE, 0):
        _err(f"this needs about {needed} API calls but Freshsales reports only {left} left in the account's "
             f"window (keeping {APPLY_RESERVE} spare); nothing was sent. Try again later")
        _emit({"mode": "apply", "sent": False, "operations": [writes.public(p) for p in plans]})
        return EXIT_FAIL

    results, ok = [], True
    for plan in plans:
        try:
            res = _apply(ctx, plan)
        except FSError as exc:
            res = {"status": "error", "error": str(exc), "verification": []}
            _err(f"{plan['op']} {plan['id']}: {exc}")
        if res["verification"]:
            _print_verification(plan, res)
        ok = ok and res["status"] in ("verified", "no-op", "deleted")
        if res["status"] == "PENDING":
            _err(f"{plan['op']} {plan['id']}: {res.get('note')}")
        if res["status"] == "deleted":
            print(f"deleted {plan['entity']} {plan['id']}: {plan.get('label')}", file=sys.stderr)
        if res["status"] == "CREATED-UNVERIFIED":
            _err(f"{plan['op']}: created as {res['created_id']} but the read-back failed: {res.get('error')}")
        results.append({**writes.public(plan), "result": res})
    _emit({"mode": "apply", "sent": True, "operations": results})
    return EXIT_OK if ok else EXIT_FAIL


def reads_split(raw):
    return [p for p in (x.strip() for x in raw.replace(";", ",").split(",")) if p]


# ---------- read commands ----------

def _run_views(client, args):
    res = reads.list_views(client, args.entity)
    _emit(res)
    print("\n" + _table(["id", "name", "default"], [[v["id"], v["name"], v["is_default"]] for v in res]),
          file=sys.stderr)
    return EXIT_OK


def _run_list_view(client, args):
    fields = _split(args.fields) if args.fields else None
    res = reads.list_view(client, args.entity, args.view_id, where=args.where, fields=fields,
                          max_pages=args.max_pages, stop_after=args.stop_after)
    _emit(res)
    cols = list(res["rows"][0].keys()) if res["rows"] else []
    if res["rows"]:
        print("\n" + _table(cols, [[r[c] for c in cols] for r in res["rows"][:args.show]]), file=sys.stderr)
    print(f"\n{res['matched']} matched of {res['rows_in_view']} in the view "
          f"({res['pages_read']} of {res['pages_in_view']} pages read)", file=sys.stderr)
    if res["truncated"]:
        print(f"WARNING: stopped early ({res['stopped']}); each page is one request of the account's "
              "1000 an hour, so raise --max-pages only if you need the rest", file=sys.stderr)
    return EXIT_OK


def _run_find_contacts(client, args):
    res = reads.find_contacts(client, name=args.name, email=args.email, city=args.city, ctype=args.type,
                              domain=args.domain)
    cols = ("id", "display_name", "email", "job_title", "city", "country")
    rows = [{c: r.get(c) for c in cols} for r in res["rows"]]
    shown = rows[:args.limit]
    out = {"total": res["total"], "returned": len(rows), "shown": len(shown), "contacts": shown,
           "notes": res["notes"]}
    if args.full:
        out["contacts"] = [reads.get_contact(client, r["id"]) for r in shown]
    _emit(out)
    if shown:
        print("\n" + _table(list(cols), [[r[c] for c in cols] for r in shown]), file=sys.stderr)
    print(f"\n{res['total']} matched; showing {len(shown)}", file=sys.stderr)
    for n in res["notes"]:
        print(f"NOTE: {n}", file=sys.stderr)
    return EXIT_OK


def _run_get(client, args):
    getter = reads.get_contact if args.cmd == "contact" else reads.get_deal
    out, failed = [], False
    for rid in _split(args.ids):
        try:
            out.append(getter(client, rid))
        except FSError as exc:
            out.append({"id": rid, "error": str(exc)})
            failed = True
    _emit(out)
    return EXIT_FAIL if failed else EXIT_OK


def _run_check_contacts(client, args):
    """Report SO-CF and completeness problems. Reads only. The one mechanical fix (P3 status) can be
    written to a batch file for update-contact; everything else is a question for Karthik."""
    sources = [bool(args.ids), bool(args.emails), bool(args.view)]
    if sum(sources) != 1:
        raise FSError("check-contacts needs exactly one of --ids, --emails or --view")
    info = {}
    if args.view:
        records, info = reads.view_rows(client, "contacts", args.view, where=args.where,
                                        max_pages=args.max_pages, stop_after=args.stop_after)
    elif args.ids:
        records = [reads.get_contact(client, i) for i in _split(args.ids)]
    else:
        records = [c for r in reads.lookup(client, _split(args.emails)) if r["status"] == "found"
                   for c in r["contacts"]]
    summary = checks.summarise(records)
    fixes = checks.p3_status_fixes(records)
    out = {"summary": {k: v for k, v in summary.items() if k != "contacts_with_issues"},
           "contacts_with_issues": summary["contacts_with_issues"], "p3_status_fixes": fixes}
    if info:
        out["view"] = info
    if args.emit_batch:
        if fixes:
            with open(args.emit_batch, "w") as fh:
                json.dump({"operations": fixes}, fh, indent=2)
        out["batch_file"] = args.emit_batch if fixes else None
    _emit(out)
    s = summary
    print(f"\nChecked {s['checked']} contacts: {s['so_cf_checked']} SO-CF "
          f"({s['so_cf_no_priority']} with no priority, {s['so_cf_bad_priority']} with a priority other than "
          f"P1/P3, {s['so_cf_p3_status_to_fix']} P3 whose status should be NA/Not Relevant); "
          f"missing city {s['missing_city']}, LinkedIn {s['missing_linkedin']}, email {s['missing_email']}",
          file=sys.stderr)
    for c in summary["contacts_with_issues"][:args.show]:
        print(f"  {c['id']} {c['name']}: " + "; ".join(c["issues"]), file=sys.stderr)
    if len(summary["contacts_with_issues"]) > args.show:
        print(f"  ... and {len(summary['contacts_with_issues']) - args.show} more (see the JSON)", file=sys.stderr)
    if info.get("truncated"):
        print(f"WARNING: only part of the view was read ({info['stopped']})", file=sys.stderr)
    if fixes:
        print(f"\n{len(fixes)} P3 contact(s) would have their Reachout Status replaced with NA/Not Relevant; "
              "the old values are in p3_status_fixes[].replaces", file=sys.stderr)
        if args.emit_batch:
            print(f"Batch written to {args.emit_batch}. Review it, then: "
                  f"fs update-contact --batch {args.emit_batch}   (dry-run first)", file=sys.stderr)
    return EXIT_OK


# ---------- parser ----------

def build_parser():
    p = argparse.ArgumentParser(prog="fs", description="Freshsales CLI (tern.freshworks.com)")
    p.add_argument("--refresh", action="store_true",
                   help="ignore cached field/picklist/stage/view metadata and fetch it again")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("lookup", help="look up contacts by email (comma-separated)")
    s.add_argument("emails")
    s.add_argument("--brief", action="store_true",
                   help="stop after the search: no custom fields, one call per 25 emails instead of one more per contact")
    s = sub.add_parser("find-deals", help="search deals and accounts (comma-separated terms)")
    s.add_argument("terms")
    s.add_argument("--details", action="store_true",
                   help="also fetch stage, amount and custom fields (one API call per hit)")
    s.add_argument("--max-details", type=int, default=10, help="hits per term to fetch with --details (default 10)")
    s = sub.add_parser("account", help="account plus linked contacts and deals")
    s.add_argument("account_id")
    s = sub.add_parser("contact", help="contacts by id, in full with deals and notes (one call each)")
    s.add_argument("ids", help="comma-separated contact ids")
    s = sub.add_parser("deal", help="deals by id, in full with contacts and notes (one call each)")
    s.add_argument("ids", help="comma-separated deal ids")
    sub.add_parser("picklists", help="valid values for the custom picklists")

    s = sub.add_parser("views", help="list saved views (ids for list-view)")
    s.add_argument("--entity", choices=sorted(reads.VIEW_ENTITIES), default="contacts")
    s = sub.add_parser("list-view", help="read a saved view and filter its rows (whole records, 100 per request)")
    s.add_argument("view_id")
    s.add_argument("--entity", choices=sorted(reads.VIEW_ENTITIES), default="contacts")
    s.add_argument("--where", action="append", metavar="FIELD=VALUE|FIELD~PART", default=[],
                   help="= whole value, ~ part of it; ignores case; commas mean any-of; FIELD may be cf_* or domain")
    s.add_argument("--fields", metavar="A,B,C", help="columns to return (row fields or cf_*)")
    s.add_argument("--max-pages", type=int, default=10, help="pages of 100 to read (default 10)")
    s.add_argument("--stop-after", type=int, metavar="N", help="stop reading once N rows match")
    s.add_argument("--show", type=int, default=50, help="rows to print in the table on stderr (default 50)")
    s = sub.add_parser("find-contacts", help="search contacts by name, email, city, type or email domain")
    s.add_argument("--name", help='"First Last" (exact) or one word (part of the surname)')
    s.add_argument("--email")
    s.add_argument("--city", help="exact; commas mean any-of")
    s.add_argument("--type", help="Contact Type picklist value; commas mean any-of")
    s.add_argument("--domain", help="email domain, on its own; free-text search stops at 100 results")
    s.add_argument("--limit", type=int, default=25, help="contacts to return (default 25)")
    s.add_argument("--full", action="store_true", help="fetch each contact in full, with custom fields (one request each)")
    s = sub.add_parser("check-contacts",
                       help="report SO-CF priority/status and city/LinkedIn/email problems (reads only)")
    s.add_argument("--ids", help="comma-separated contact ids (one call each)")
    s.add_argument("--emails", help="comma-separated emails (one search per 25, then one call per contact)")
    s.add_argument("--view", help="saved view id: 100 contacts per call, the cheapest way to check many")
    s.add_argument("--where", action="append", metavar="FIELD=VALUE|FIELD~PART", default=[],
                   help="with --view: only check matching rows (same syntax as list-view)")
    s.add_argument("--max-pages", type=int, default=10)
    s.add_argument("--stop-after", type=int, metavar="N")
    s.add_argument("--emit-batch", metavar="FILE", help="write the P3 status fixes as an update-contact batch file")
    s.add_argument("--show", type=int, default=50, help="contacts with issues to list on stderr (default 50)")

    for name in ("update-contact", "update-deal", "update-account"):
        s = sub.add_parser(name, help=f"{name} (dry-run unless --apply)")
        s.add_argument("id", nargs="?")
        s.add_argument("--set", action="append", metavar="KEY=VALUE", default=[])
        s.add_argument("--batch", metavar="FILE", help="JSON list of {id, set} operations")
        s.add_argument("--apply", action="store_true", help="actually write")
    for entity in ("contact", "deal", "account"):
        s = sub.add_parser(f"create-{entity}", help=f"create a {entity} (dry-run unless --apply)")
        s.add_argument("--set", action="append", metavar="KEY=VALUE", default=[])
        s.add_argument("--preset", choices=creates.preset_names(entity),
                       help="skill defaults, shown in the diff as 'default'; --set overrides them")
        s.add_argument("--batch", metavar="FILE", help='JSON list of {"set": {...}, "preset": "..."}')
        s.add_argument("--apply", action="store_true", help="actually write")
    for entity, guard in (("contact", "refused if it is on a live deal or has notes"),
                          ("deal", "refused if it has notes"),
                          ("account", "refused while it has contacts or deals")):
        s = sub.add_parser(f"delete-{entity}", help=f"PERMANENTLY delete {entity}s; {guard} (dry-run unless --apply)")
        s.add_argument("ids", nargs="*", help="ids (space or comma separated)")
        s.add_argument("--apply", action="store_true", help="actually delete")
    s = sub.add_parser("merge-contacts", help="PERMANENTLY merge duplicate contacts into one (dry-run unless --apply)")
    s.add_argument("id", help="the contact to keep")
    s.add_argument("--with", dest="with_ids", metavar="IDS", help="duplicate contact ids to merge in, comma-separated")
    s.add_argument("--apply", action="store_true", help="actually merge")
    s = sub.add_parser("add-note", help="add a note to a contact, deal or account (dry-run unless --apply)")
    s.add_argument("id", nargs="?")
    s.add_argument("text", nargs="?")
    s.add_argument("--on", choices=sorted(writes.NOTE_TARGETS), default="contact",
                   help="what the id is (default: contact)")
    s.add_argument("--batch", metavar="FILE", help='JSON list of {"id", "text", "on"} operations')
    s.add_argument("--apply", action="store_true", help="actually write")
    s = sub.add_parser("link-deal-contacts",
                       help="add and/or remove contacts on a deal (dry-run unless --apply)")
    s.add_argument("id", nargs="?", help="deal id")
    s.add_argument("--add", metavar="IDS", help="contact ids to link, comma-separated")
    s.add_argument("--remove", metavar="IDS", help="contact ids to unlink, comma-separated")
    s.add_argument("--batch", metavar="FILE", help='JSON list of {"id", "add": [...], "remove": [...]}')
    s.add_argument("--apply", action="store_true", help="actually write")
    return p


def _dispatch(client, args):
    if args.cmd == "lookup":
        res = reads.lookup(client, _split(args.emails), brief=args.brief)
        _emit(res)
        return EXIT_FAIL if any(r["status"] == "error" for r in res) else EXIT_OK
    if args.cmd == "find-deals":
        res = reads.find_deals(client, _split(args.terms), details=args.details, max_details=args.max_details)
        _emit(res)
        return EXIT_FAIL if any("error" in r for r in res) else EXIT_OK
    if args.cmd == "account":
        _emit(reads.get_account(client, args.account_id))
        return EXIT_OK
    if args.cmd in ("contact", "deal"):
        return _run_get(client, args)
    if args.cmd == "views":
        return _run_views(client, args)
    if args.cmd == "list-view":
        return _run_list_view(client, args)
    if args.cmd == "find-contacts":
        return _run_find_contacts(client, args)
    if args.cmd == "check-contacts":
        return _run_check_contacts(client, args)
    if args.cmd == "picklists":
        res = reads.picklists(client)
        _emit(res)
        return EXIT_FAIL if any("error" in v for v in res.values()) else EXIT_OK
    return run_write(client, args, args.cmd)


def main(argv=None, client=None):
    args = build_parser().parse_args(argv)
    try:
        client = client or Client(load_api_key(), disk_cache=MetaCache.from_env(refresh=args.refresh))
    except FSError as exc:
        _err(str(exc))
        return EXIT_FAIL
    try:
        return _dispatch(client, args)
    except FSError as exc:
        _err(str(exc))
        return EXIT_FAIL
    finally:
        usage = client.usage()
        if usage:
            print(usage, file=sys.stderr)
