"""Command-line interface for `fs`."""
import argparse
import json
import sys

from . import creates, reads, writes
from .client import Client, FSError, load_api_key

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


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


def _plan(ctx, op, item):
    if op.startswith("create-"):
        sets = {k: str(v) for k, v in (item.get("set") or {}).items()}
        return creates.plan_create(ctx, op.split("-")[1], sets, item.get("preset"))
    if op == "update-contact":
        sets = item.get("set")
        if not isinstance(sets, dict):
            raise FSError(f"{op} {item['id']}: 'set' must be an object")
        return writes.plan_update_contact(ctx, item["id"], {k: str(v) for k, v in sets.items()})
    if op == "update-deal":
        sets = item.get("set")
        if not isinstance(sets, dict):
            raise FSError(f"{op} {item['id']}: 'set' must be an object")
        return writes.plan_update_deal(ctx, item["id"], {k: str(v) for k, v in sets.items()})
    if op == "link-deal-contacts":
        def ids(v):
            return ",".join(str(x) for x in v) if isinstance(v, list) else v
        return writes.plan_link_deal_contacts(ctx, item["id"], ids(item.get("add")), ids(item.get("remove")))
    return writes.plan_add_note(ctx, item["id"], item.get("text"), item.get("on") or "contact")


def run_write(client, args, op):
    ops = _collect_ops(args, op)
    ctx = writes.Context(client)

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

    results, ok = [], True
    for plan in plans:
        try:
            res = (creates.apply_create(ctx, plan) if plan["op"].startswith("create-")
                   else writes.apply_plan(ctx, plan))
        except FSError as exc:
            res = {"status": "error", "error": str(exc), "verification": []}
            _err(f"{plan['op']} {plan['id']}: {exc}")
        if res["verification"]:
            _print_verification(plan, res)
        ok = ok and res["status"] in ("verified", "no-op")
        if res["status"] == "CREATED-UNVERIFIED":
            _err(f"{plan['op']}: created as {res['created_id']} but the read-back failed: {res.get('error')}")
        results.append({**writes.public(plan), "result": res})
    _emit({"mode": "apply", "sent": True, "operations": results})
    return EXIT_OK if ok else EXIT_FAIL


# ---------- parser ----------

def _run_views(client, args):
    res = reads.list_views(client, args.entity)
    _emit(res)
    print("\n" + _table(["id", "name", "default"], [[v["id"], v["name"], v["is_default"]] for v in res]),
          file=sys.stderr)
    return EXIT_OK


def _run_list_view(client, args):
    fields = _split(args.fields) if args.fields else None
    res = reads.list_view(client, args.entity, args.view_id, where=args.where, fields=fields,
                          max_pages=args.max_pages)
    _emit(res)
    cols = list(res["rows"][0].keys()) if res["rows"] else []
    if res["rows"]:
        print("\n" + _table(cols, [[r[c] for c in cols] for r in res["rows"][:args.show]]), file=sys.stderr)
    print(f"\n{res['matched']} matched of {res['rows_in_view']} in the view "
          f"({res['pages_read']} of {res['pages_in_view']} pages read)", file=sys.stderr)
    if res["truncated"]:
        print(f"WARNING: stopped after {args.max_pages} pages; raise --max-pages to read the rest "
              "(each page is one request of the account's 1000 an hour)", file=sys.stderr)
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


def build_parser():
    p = argparse.ArgumentParser(prog="fs", description="Freshsales CLI (tern.freshworks.com)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("lookup", help="look up contacts by email (comma-separated)")
    s.add_argument("emails")
    s = sub.add_parser("find-deals", help="search deals and accounts (comma-separated terms)")
    s.add_argument("terms")
    s = sub.add_parser("account", help="account plus linked contacts and deals")
    s.add_argument("account_id")
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
    s.add_argument("--show", type=int, default=50, help="rows to print in the table on stderr (default 50)")
    s = sub.add_parser("find-contacts", help="search contacts by name, email, city, type or email domain")
    s.add_argument("--name", help='"First Last" (exact) or one word (part of the surname)')
    s.add_argument("--email")
    s.add_argument("--city", help="exact; commas mean any-of")
    s.add_argument("--type", help="Contact Type picklist value; commas mean any-of")
    s.add_argument("--domain", help="email domain, on its own; free-text search stops at 100 results")
    s.add_argument("--limit", type=int, default=25, help="contacts to return (default 25)")
    s.add_argument("--full", action="store_true", help="fetch each contact in full, with custom fields (one request each)")

    for name in ("update-contact", "update-deal"):
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


def main(argv=None, client=None):
    args = build_parser().parse_args(argv)
    try:
        client = client or Client(load_api_key())
        if args.cmd == "lookup":
            res = reads.lookup(client, _split(args.emails))
            _emit(res)
            return EXIT_FAIL if any(r["status"] == "error" for r in res) else EXIT_OK
        if args.cmd == "find-deals":
            res = reads.find_deals(client, _split(args.terms))
            _emit(res)
            return EXIT_FAIL if any("error" in r for r in res) else EXIT_OK
        if args.cmd == "account":
            _emit(reads.get_account(client, args.account_id))
            return EXIT_OK
        if args.cmd == "views":
            return _run_views(client, args)
        if args.cmd == "list-view":
            return _run_list_view(client, args)
        if args.cmd == "find-contacts":
            return _run_find_contacts(client, args)
        if args.cmd == "picklists":
            res = reads.picklists(client)
            _emit(res)
            return EXIT_FAIL if any("error" in v for v in res.values()) else EXIT_OK
        return run_write(client, args, args.cmd)
    except FSError as exc:
        _err(str(exc))
        return EXIT_FAIL
