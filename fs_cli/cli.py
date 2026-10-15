"""Command-line interface for `fs`."""
import argparse
import json
import sys

from . import reads, writes
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
    print(f"\n== {plan['op']} {plan['entity']} {plan['id']} ({plan.get('label') or '?'}) ==",
          file=sys.stderr)
    rows = []
    for ch in plan["changes"]:
        flag = "OVERWRITE" if ch["overwrites_existing"] else ("change" if ch["changed"] else "same")
        rows.append([ch["field"], _fmt(ch["before"]), _fmt(ch["after"]), flag])
    if rows:
        print(_table(["field", "before", "after", ""], rows), file=sys.stderr)
    for e in plan["errors"]:
        print(f"  INVALID: {e}", file=sys.stderr)


def _print_verification(plan, result):
    rows = [[r["field"], _fmt(r["intended"]), _fmt(r["actual"]), "ok" if r["ok"] else "DID NOT TAKE"]
            for r in result["verification"]]
    print(f"\n-- verification {plan['entity']} {plan['id']}: {result['status']} --", file=sys.stderr)
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
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "id" not in item:
            raise FSError(f"batch item {i}: must be an object with an 'id'")
        if item.get("op", op) != op:
            raise FSError(f"batch item {i}: op {item.get('op')!r} does not match command {op!r}")
        ops.append(item)
    return ops


def _collect_ops(args, op):
    if args.batch:
        if getattr(args, "id", None):
            raise FSError("give either an id or --batch, not both")
        return _load_batch(args.batch, op)
    if not args.id:
        raise FSError(f"{op} needs an id (or --batch FILE)")
    if op == "add-note":
        if args.text is None:
            raise FSError("add-note needs the note text")
        return [{"id": args.id, "text": args.text}]
    return [{"id": args.id, "set": writes.parse_sets(args.set)}]


def _plan(ctx, op, item):
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
    return writes.plan_add_note(ctx, item["id"], item.get("text"))


def run_write(client, args, op):
    ops = _collect_ops(args, op)
    ctx = writes.Context(client)

    plans, failed = [], False
    for item in ops:
        try:
            plan = _plan(ctx, op, item)
        except FSError as exc:
            plan = {"op": op, "entity": op.split("-")[1], "id": item.get("id"), "label": None,
                    "changes": [], "errors": [str(exc)], "request": None, "_checks": []}
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
            res = writes.apply_plan(ctx, plan)
        except FSError as exc:
            res = {"status": "error", "error": str(exc), "verification": []}
            _err(f"{plan['op']} {plan['id']}: {exc}")
        if res["verification"]:
            _print_verification(plan, res)
        ok = ok and res["status"] in ("verified", "no-op")
        results.append({**writes.public(plan), "result": res})
    _emit({"mode": "apply", "sent": True, "operations": results})
    return EXIT_OK if ok else EXIT_FAIL


# ---------- parser ----------

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

    for name in ("update-contact", "update-deal"):
        s = sub.add_parser(name, help=f"{name} (dry-run unless --apply)")
        s.add_argument("id", nargs="?")
        s.add_argument("--set", action="append", metavar="KEY=VALUE", default=[])
        s.add_argument("--batch", metavar="FILE", help="JSON list of {id, set} operations")
        s.add_argument("--apply", action="store_true", help="actually write")
    s = sub.add_parser("add-note", help="add a note to a contact (dry-run unless --apply)")
    s.add_argument("id", nargs="?")
    s.add_argument("text", nargs="?")
    s.add_argument("--batch", metavar="FILE", help="JSON list of {id, text} operations")
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
        if args.cmd == "picklists":
            res = reads.picklists(client)
            _emit(res)
            return EXIT_FAIL if any("error" in v for v in res.values()) else EXIT_OK
        return run_write(client, args, args.cmd)
    except FSError as exc:
        _err(str(exc))
        return EXIT_FAIL
