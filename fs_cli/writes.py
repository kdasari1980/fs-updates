"""Write operations: plan (dry-run diff), validate, apply, verify.

Every write is first turned into a plan by reading the current record. The
plan is all a dry run shows. Applying re-validates, sends exactly the
plan's payload, re-reads the record and checks every intended field took.
"""
import re

from . import reads, values
from .client import FSError, NotFound

CONTACT_STANDARD = ("first_name", "last_name", "job_title", "mobile_number", "work_number", "linkedin",
                    "city", "country", "address", "state", "zipcode")
CONTACT_ACCOUNT_KEYS = ("sales_account", "account")
DEAL_STANDARD = ("name", "amount", "expected_close", "probability")
NUMERIC_FIELDS = ("amount", "probability")
# note target -> (settings/path name, targetable_type, record key in responses)
NOTE_TARGETS = {"contact": ("contacts", "Contact", "contact"),
                "deal": ("deals", "Deal", "deal"),
                "account": ("sales_accounts", "SalesAccount", "sales_account")}


class Context:
    """Per-run cache of field metadata so batches don't refetch it."""

    def __init__(self, client):
        self.client = client
        self._fields = {}
        self._stages = None
        self.seen = set()  # emails and names planned so far in this run (creates)

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


PHONE_FIELDS = ("mobile_number", "work_number")
DATE_FIELDS = ("expected_close",)


def same(field, a, b):
    if _empty(a) and _empty(b):
        return True
    if _empty(a) or _empty(b):
        return False
    if field in PHONE_FIELDS:
        return values.compare("phone", b, a)
    if field in DATE_FIELDS:
        return values.compare("date", b, a)
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


def _typed_change(field, before, payload_value, kind, expected):
    changed = not values.compare(kind, expected, before)
    had_value = bool(before) if kind == "bool" else not _empty(before)  # an unticked box is not a value
    return {"field": field, "before": before, "after": payload_value, "changed": changed,
            "overwrites_existing": changed and had_value}


def _custom_update(ctx, entity, fields_entity, key, value, current_custom, errors, changes, custom, checks):
    """Shared cf_* handling for contacts and deals: validate by field type, diff kind-aware."""
    before = current_custom.get(key)
    if _empty(value):  # `key=` clears the field
        if fields_entity and ctx.fields(fields_entity).get(key) is None:
            errors.append(f"{key}: no such custom field on {entity}")
            return
        ch = _change(key, before, "")
        changes.append(ch)
        if ch["changed"]:
            custom[key] = ""
            checks.append(("cf", key, ""))
        return
    res = values.custom(ctx, fields_entity, key, value, errors, entity)
    if res is None:
        return
    pvalue, kind, expected = res
    ch = _typed_change(key, before, pvalue, kind, expected)
    changes.append(ch)
    if ch["changed"]:
        custom[key] = pvalue
        checks.append(("cf", key, pvalue, kind, expected))


def _owner_update(ctx, entity, fields_entity, value, current, errors, changes, payload, checks):
    oid = values.choice(ctx, fields_entity, "owner_id", value, errors, entity)
    if oid is None:
        return
    before = current.get("owner_id")
    name = values.choice_name(ctx, fields_entity, "owner_id", oid)
    ch = _change("owner_id", before, oid,
                 values.choice_name(ctx, fields_entity, "owner_id", before) if before else None,
                 f"{name} ({oid})")
    changes.append(ch)
    if ch["changed"]:
        payload["owner_id"] = oid
        checks.append(("std", "owner_id", oid))


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
        elif key.startswith("cf_"):
            _custom_update(ctx, "contact", "contacts", key, value, current["custom_field"],
                           errors, changes, custom, checks)
        elif key in ("owner", "owner_id"):
            _owner_update(ctx, "contact", "contacts", value, current, errors, changes, payload, checks)
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
                          f"{list(CONTACT_STANDARD) + ['owner', 'sales_account', 'cf_*']}")

    if custom:
        payload["custom_field"] = custom
    return {
        "op": "update-contact", "entity": "contact", "id": contact_id,
        "label": current.get("display_name"), "changes": changes, "errors": errors,
        "request": {"method": "PUT", "path": f"contacts/{contact_id}",
                    "body": {"contact": payload}} if payload else None,
        "_checks": checks,
    }


def _note_label(ctx, on, record_id):
    if on == "contact":
        return reads.get_contact(ctx.client, record_id).get("display_name")
    if on == "deal":
        return reads.get_deal(ctx.client, record_id).get("name")
    return reads.get_account_basic(ctx.client, record_id).get("name")


def plan_add_note(ctx, record_id, text, on="contact"):
    if on not in NOTE_TARGETS:
        raise FSError(f"add-note --on must be one of {sorted(NOTE_TARGETS)}, got {on!r}")
    if _empty(text):
        raise FSError("add-note needs non-empty text")
    _, targetable_type, _ = NOTE_TARGETS[on]
    label = _note_label(ctx, on, record_id)
    return {
        "op": "add-note", "entity": on, "id": record_id, "label": label,
        "changes": [_change("note (new)", None, text)],
        "errors": [], "warnings": [],
        "request": {"method": "POST", "path": "notes",
                    "body": {"note": {"description": text, "targetable_type": targetable_type,
                                      "targetable_id": int(record_id) if str(record_id).isdigit()
                                      else record_id}}},
        "_checks": [("note", "note", text)],
    }


def _id_list(raw, what, errors):
    out = []
    for part in values.split(raw or ""):
        if not part.isdigit():
            errors.append(f"{what}: {part!r} is not a contact id")
        elif int(part) not in out:
            out.append(int(part))
    return out


def plan_link_deal_contacts(ctx, deal_id, add, remove):
    """Add and/or remove contacts on a deal.

    On the live API `contacts_added_list` appends (ids already linked are harmless) and
    `contacts_removed_list` removes, so only the changes are sent, not the whole list.
    """
    errors, warnings, changes, checks = [], [], [], []
    add_ids, remove_ids = _id_list(add, "add", errors), _id_list(remove, "remove", errors)
    if not add_ids and not remove_ids and not errors:
        raise FSError("link-deal-contacts needs --add and/or --remove (comma-separated contact ids)")
    both = sorted(set(add_ids) & set(remove_ids))
    if both:
        errors.append(f"contacts {both} are in both --add and --remove")
    deal = reads.get_deal(ctx.client, deal_id)
    linked = reads.deal_contacts(ctx.client, deal_id)
    names = {c["id"]: c.get("display_name") for c in linked}
    linked_ids = [c["id"] for c in linked]

    to_add, to_remove = [], []
    for cid in add_ids:
        if cid in linked_ids:
            changes.append(_change(f"contact {cid}", "linked", "linked", f"linked ({names[cid]})",
                                   f"linked ({names[cid]})"))
            continue
        try:
            name = reads.get_contact(ctx.client, cid).get("display_name")
        except NotFound:
            errors.append(f"add: contact {cid} does not exist")
            continue
        to_add.append(cid)
        changes.append(_change(f"contact {cid}", None, "linked", "(not linked)", f"linked ({name})"))
    for cid in remove_ids:
        if cid not in linked_ids:
            warnings.append(f"remove: contact {cid} is not linked to this deal; nothing to remove")
            continue
        to_remove.append(cid)
        ch = _change(f"contact {cid}", "linked", None, f"linked ({names[cid]})", "(removed)")
        ch["overwrites_existing"] = True
        changes.append(ch)

    final = [c for c in linked_ids + to_add if c not in to_remove]
    if not final and (to_add or to_remove):
        warnings.append("the deal would be left with no contacts")
    body = {}
    if to_add:
        body["contacts_added_list"] = to_add
    if to_remove:
        body["contacts_removed_list"] = to_remove
    return {
        "op": "link-deal-contacts", "entity": "deal", "id": deal_id, "label": deal.get("name"),
        "changes": changes, "errors": errors, "warnings": warnings,
        "request": {"method": "PUT", "path": f"deals/{deal_id}", "body": {"deal": body}} if body else None,
        "_checks": [("contacts_exact", "contacts", final)],
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
            _custom_update(ctx, "deal", "deals", key, value, current["custom_field"],
                           errors, changes, custom, checks)
        elif key in ("owner", "owner_id"):
            _owner_update(ctx, "deal", "deals", value, current, errors, changes, payload, checks)
        else:
            errors.append(f"{key}: not a supported deal field. Supported: "
                          f"{list(DEAL_STANDARD) + ['deal_stage', 'owner', 'sales_account', 'cf_*']}")

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
    if plan["op"] == "link-deal-contacts":
        actual = [c["id"] for c in reads.deal_contacts(ctx.client, plan["id"])]
        want = checks[0][2]
        return [{"field": "contacts", "intended": want, "actual": actual,
                 "ok": values.compare("exact", want, actual)}]

    if plan["op"] == "add-note":
        note_id = ((response or {}).get("note") or {}).get("id")
        path, _, key = NOTE_TARGETS[plan["entity"]]
        data = ctx.client.get(f"{path}/{plan['id']}", params={"include": "notes"})
        notes = reads._included(data, data.get(key) or {}, "note", "notes")
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
    for chk in checks:
        kind, field, intended = chk[:3]
        if kind == "cf" and len(chk) > 3:
            actual = after["custom_field"].get(field)
            rows.append({"field": field, "intended": intended, "actual": actual,
                         "ok": values.compare(chk[3], chk[4], actual)})
        elif kind == "std":
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
