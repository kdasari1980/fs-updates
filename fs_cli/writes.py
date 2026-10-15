"""Write operations: plan (dry-run diff), validate, apply, verify.

Every write is first turned into a plan by reading the current record. The
plan is all a dry run shows. Applying re-validates, sends exactly the
plan's payload, re-reads the record and checks every intended field took.
"""
import re

from . import reads
from .client import FSError

CONTACT_STANDARD = ("first_name", "last_name", "job_title", "mobile_number", "linkedin", "city")
CONTACT_CUSTOM = reads.PICKLIST_FIELDS
CONTACT_ACCOUNT_KEYS = ("sales_account", "account")
DEAL_STANDARD = ("name", "amount", "expected_close", "probability")
NUMERIC_FIELDS = ("amount", "probability")


class Context:
    """Per-run cache of field metadata so batches don't refetch it."""

    def __init__(self, client):
        self.client = client
        self._fields = {}
        self._stages = None

    def fields(self, entity):
        if entity not in self._fields:
            self._fields[entity] = {f.get("name"): f for f in reads.get_fields(self.client, entity)}
        return self._fields[entity]

    def stages(self):
        if self._stages is None:
            self._stages = reads.deal_stages(self.client)
        return self._stages


def parse_sets(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise FSError(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise FSError(f"--set has an empty key: {pair!r}")
        out[key] = value
    return out


def _empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "") or v == []


def same(field, a, b):
    if _empty(a) and _empty(b):
        return True
    if _empty(a) or _empty(b):
        return False
    if field in NUMERIC_FIELDS:
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            pass
    return str(a).strip() == str(b).strip()


def _change(field, before, after, display_before=None, display_after=None):
    db = before if display_before is None else display_before
    da = after if display_after is None else display_after
    changed = not same(field, before, after)
    return {
        "field": field,
        "before": db,
        "after": da,
        "changed": changed,
        "overwrites_existing": changed and not _empty(before),
    }


def _validate_picklist(ctx, entity, field, value, errors):
    if _empty(value):
        return
    meta = ctx.fields(entity).get(field)
    if meta is None:
        errors.append(f"{field}: no such field on {entity}")
        return
    valid = reads.choice_values(meta)
    if valid and value not in valid:
        errors.append(f"{field}: {value!r} is not a valid picklist value. Valid: {valid}")


# ---------- plans ----------

def plan_update_contact(ctx, contact_id, sets):
    if not sets:
        raise FSError("update-contact needs at least one --set key=value")
    current = reads.get_contact(ctx.client, contact_id)
    errors, changes, payload, custom, checks = [], [], {}, {}, []

    for key, value in sets.items():
        if key in CONTACT_STANDARD:
            ch = _change(key, current.get(key), value)
            changes.append(ch)
            if ch["changed"]:
                payload[key] = value
                checks.append(("std", key, value))
        elif key in CONTACT_CUSTOM:
            _validate_picklist(ctx, "contacts", key, value, errors)
            ch = _change(key, current["custom_field"].get(key), value)
            changes.append(ch)
            if ch["changed"]:
                custom[key] = value
                checks.append(("cf", key, value))
        elif key in CONTACT_ACCOUNT_KEYS:
            existing = current["sales_accounts"]
            before_names = [a.get("name") for a in existing]
            try:
                target = reads.find_account_by_exact_name(ctx.client, value)
            except FSError as exc:
                errors.append(f"sales_account: {exc}")
                changes.append(_change("sales_account", before_names, value))
                continue
            already_primary = any(str(a.get("id")) == str(target["id"]) and a.get("is_primary")
                                  for a in existing)
            linked = any(str(a.get("id")) == str(target["id"]) for a in existing)
            if linked and (already_primary or len(existing) == 1):
                changes.append(_change("sales_account", target["name"], target["name"],
                                       before_names, before_names))
                continue
            # Send the full list (new account primary, existing kept) because the
            # docs don't say whether a PUT replaces or appends associations.
            new_list = [{"id": target["id"], "is_primary": True}] + [
                {"id": a["id"], "is_primary": False}
                for a in existing if str(a.get("id")) != str(target["id"])]
            after_names = [f"{target['name']} (primary)"] + [
                a.get("name") for a in existing if str(a.get("id")) != str(target["id"])]
            ch = _change("sales_account", before_names, after_names)
            ch["overwrites_existing"] = bool(existing)
            changes.append(ch)
            payload["sales_accounts"] = new_list
            checks.append(("account", "sales_account", target))
        else:
            errors.append(f"{key}: not a supported contact field. Supported: "
                          f"{list(CONTACT_STANDARD) + list(CONTACT_CUSTOM) + ['sales_account']}")

    if custom:
        payload["custom_field"] = custom
    return {
        "op": "update-contact", "entity": "contact", "id": contact_id,
        "label": current.get("display_name"), "changes": changes, "errors": errors,
        "request": {"method": "PUT", "path": f"contacts/{contact_id}",
                    "body": {"contact": payload}} if payload else None,
        "_checks": checks,
    }


def plan_add_note(ctx, contact_id, text):
    if _empty(text):
        raise FSError("add-note needs non-empty text")
    current = reads.get_contact(ctx.client, contact_id)
    return {
        "op": "add-note", "entity": "contact", "id": contact_id,
        "label": current.get("display_name"),
        "changes": [_change("note (new)", None, text)],
        "errors": [],
        "request": {"method": "POST", "path": "notes",
                    "body": {"note": {"description": text, "targetable_type": "Contact",
                                      "targetable_id": int(contact_id) if str(contact_id).isdigit()
                                      else contact_id}}},
        "_checks": [("note", "note", text)],
    }


def _resolve_stage(ctx, name, pipeline_id):
    matches = [s for s in ctx.stages() if (s.get("name") or "").strip() == name.strip()]
    if len(matches) > 1 and pipeline_id is not None:
        matches = [s for s in matches if str(s.get("deal_pipeline_id")) == str(pipeline_id)]
    if not matches:
        valid = sorted({s.get("name") for s in ctx.stages()
                        if pipeline_id is None or str(s.get("deal_pipeline_id")) == str(pipeline_id)})
        raise FSError(f"deal_stage: {name!r} is not a valid stage. Valid: {valid}")
    if len(matches) > 1:
        raise FSError(f"deal_stage: {name!r} is ambiguous across pipelines")
    return matches[0]


def plan_update_deal(ctx, deal_id, sets):
    if not sets:
        raise FSError("update-deal needs at least one --set key=value")
    current = reads.get_deal(ctx.client, deal_id)
    errors, changes, payload, custom, checks = [], [], {}, {}, []

    for key, value in sets.items():
        if key in DEAL_STANDARD:
            ch = _change(key, current.get(key), value)
            changes.append(ch)
            if ch["changed"]:
                payload[key] = value
                checks.append(("std", key, value))
        elif key in ("deal_stage", "stage"):
            try:
                stage = _resolve_stage(ctx, value, current.get("deal_pipeline_id"))
            except FSError as exc:
                errors.append(str(exc))
                changes.append(_change("deal_stage", current.get("stage"), value))
                continue
            ch = _change("deal_stage", current.get("deal_stage_id"), stage["id"],
                         current.get("stage"), stage.get("name"))
            changes.append(ch)
            if ch["changed"]:
                payload["deal_stage_id"] = stage["id"]
                checks.append(("std", "deal_stage_id", stage["id"]))
        elif key in ("sales_account", "account"):
            before = current.get("sales_account") or {}
            try:
                target = reads.find_account_by_exact_name(ctx.client, value)
            except FSError as exc:
                errors.append(f"sales_account: {exc}")
                changes.append(_change("sales_account", before.get("name"), value))
                continue
            ch = _change("sales_account", before.get("id"), target["id"],
                         before.get("name"), target["name"])
            changes.append(ch)
            if ch["changed"]:
                payload["sales_account_id"] = target["id"]
                checks.append(("std", "sales_account_id", target["id"]))
        elif key.startswith("cf_"):
            if key not in ctx.fields("deals"):
                errors.append(f"{key}: no such custom field on deals")
                continue
            _validate_picklist(ctx, "deals", key, value, errors)
            ch = _change(key, current["custom_field"].get(key), value)
            changes.append(ch)
            if ch["changed"]:
                custom[key] = value
                checks.append(("cf", key, value))
        else:
            errors.append(f"{key}: not a supported deal field. Supported: "
                          f"{list(DEAL_STANDARD) + ['deal_stage', 'sales_account', 'cf_*']}")

    if custom:
        payload["custom_field"] = custom
    return {
        "op": "update-deal", "entity": "deal", "id": deal_id,
        "label": current.get("name"), "changes": changes, "errors": errors,
        "request": {"method": "PUT", "path": f"deals/{deal_id}",
                    "body": {"deal": payload}} if payload else None,
        "_checks": checks,
    }


# ---------- apply + verify ----------

def _strip_html(text):
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _verify(ctx, plan, response):
    rows = []
    checks = plan["_checks"]
    if plan["op"] == "add-note":
        note_id = ((response or {}).get("note") or {}).get("id")
        data = ctx.client.get(f"contacts/{plan['id']}", params={"include": "notes"})
        notes = reads._included(data, data.get("contact") or {}, "note", "notes")
        found = next((n for n in notes if str(n.get("id")) == str(note_id)), None)
        intended = checks[0][2]
        row = {"field": "note", "intended": intended,
               "actual": found.get("description") if found else None}
        if found is None and not notes and note_id is not None:
            # Contact read did not include notes at all; fall back to the POST echo.
            row["actual"] = ((response or {}).get("note") or {}).get("description")
            row["source"] = "POST response (notes not included on contact read)"
        row["ok"] = row["actual"] is not None and _strip_html(row["actual"]) == intended.strip()
        rows.append(row)
        return rows

    if plan["op"] == "update-contact":
        after = reads.get_contact(ctx.client, plan["id"])
    else:
        after = reads.get_deal(ctx.client, plan["id"])
    for kind, field, intended in checks:
        if kind == "std":
            actual = after.get(field)
            rows.append({"field": field, "intended": intended, "actual": actual,
                         "ok": same(field, actual, intended)})
        elif kind == "cf":
            actual = after["custom_field"].get(field)
            rows.append({"field": field, "intended": intended, "actual": actual,
                         "ok": same(field, actual, intended)})
        elif kind == "account":
            accs = after["sales_accounts"]
            match = next((a for a in accs if str(a.get("id")) == str(intended["id"])), None)
            primary_ok = match is not None and match.get("is_primary", True) is not False
            rows.append({"field": "sales_account",
                         "intended": f"{intended['name']} (primary)",
                         "actual": [f"{a.get('name')}{' (primary)' if a.get('is_primary') else ''}"
                                    for a in accs],
                         "ok": primary_ok})
    return rows


def apply_plan(ctx, plan):
    req = plan["request"]
    if req is None:
        return {"status": "no-op", "verification": []}
    if req["method"] == "PUT":
        response = ctx.client.put(req["path"], req["body"])
    else:
        response = ctx.client.post(req["path"], req["body"])
    rows = _verify(ctx, plan, response)
    return {"status": "verified" if all(r["ok"] for r in rows) else "MISMATCH",
            "verification": rows}


def public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}
