"""Write operations: plan (dry-run diff), validate, apply, verify.

Every write is first turned into a plan by reading the current record. The
plan is all a dry run shows. Applying re-validates, sends exactly the
plan's payload, re-reads the record and checks every intended field took.
"""
import re

from . import checks as rules
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
        self.email_hits = {}      # lowercased email -> existing contacts, filled by one batched search
        self.email_checked = set()  # emails that search has already covered

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

def _email_update(ctx, contact_id, current, value, errors, changes, payload, checks):
    """Make `value` the primary email and keep the old addresses as secondary.

    A PUT replaces the whole list (anything left out is removed), so every existing address is sent back.
    """
    new = str(value).strip()
    if not values.EMAIL_RE.match(new):
        errors.append(f"email: {value!r} is not a valid email address")
        return
    emails = current.get("emails") or []
    existing = next((e for e in emails if (e.get("value") or "").lower() == new.lower()), None)
    before = ", ".join(f"{e['value']}{' (primary)' if e.get('is_primary') else ''}" for e in emails) or None
    if existing and existing.get("is_primary"):
        changes.append(_change("email", before, before))
        return
    if not existing:
        for hit in reads.contacts_by_email(ctx.client, new):
            if str(hit["id"]) != str(contact_id):
                errors.append(f"email: {new} already belongs to contact {hit['id']} ({hit.get('display_name')}). If "
                              "that is a blank duplicate made by an email BCC, merge it with "
                              f"merge-contacts {contact_id} --with {hit['id']}")
    primary = ({"id": existing["id"], "value": existing["value"], "is_primary": True} if existing
               else {"value": new, "is_primary": True})
    others = [e for e in emails if e is not existing]
    payload["emails"] = [primary] + [{"id": e["id"], "value": e["value"], "is_primary": False} for e in others]
    after = f"{new} (primary)" + "".join(f", {e['value']}" for e in others)
    ch = _change("email", before, after)
    ch["overwrites_existing"] = any(e.get("is_primary") for e in emails)
    changes.append(ch)
    checks.append(("emails", "email", new, [e["value"] for e in emails]))


def _contact_after(current, payload, custom):
    """The contact as it will stand once the update is applied, for the rule checks."""
    after = dict(current)
    for k, v in payload.items():
        if k not in ("custom_field", "emails", "sales_accounts"):
            after[k] = v
    after["custom_field"] = {**(current.get("custom_field") or {}), **custom}
    if "emails" in payload:
        after["emails"] = [{"value": e["value"]} for e in payload["emails"]]
        after["email"] = payload["emails"][0]["value"]
    return after


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
        elif key == "email":
            _email_update(ctx, contact_id, current, value, errors, changes, payload, checks)
        elif key == "subscription_types":
            ids = [values.choice(ctx, "contacts", "subscription_types", part, errors, "contact")
                   for part in values.split(value)]
            ids = [str(i) for i in ids if i is not None]
            if ids:
                joined = ";".join(ids)
                ch = _typed_change(key, current.get("subscription_types"), f"{value} ({joined})", "set", set(ids))
                changes.append(ch)
                if ch["changed"]:
                    payload["subscription_types"] = joined
                    checks.append(("std", key, f"{value} ({joined})", "set", set(ids)))
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
                          f"{list(CONTACT_STANDARD) + ['email', 'owner', 'subscription_types', 'sales_account', 'cf_*']}")

    if custom:
        payload["custom_field"] = custom
    return {
        "op": "update-contact", "entity": "contact", "id": contact_id,
        "label": current.get("display_name"), "changes": changes, "errors": errors,
        "warnings": [m for _, m in rules.contact_issues(_contact_after(current, payload, custom))],
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
    deal = reads.get_deal(ctx.client, deal_id)   # one call: the deal and its contacts together
    linked = deal["contacts"]
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


def plan_update_account(ctx, account_id, sets):
    """Rename an account, change its address, owner or custom fields (such as Account Type)."""
    if not sets:
        raise FSError("update-account needs at least one --set key=value")
    current = reads.account_from({"sales_account": reads.get_account(ctx.client, account_id)["account"]})
    errors, changes, payload, custom, checks = [], [], {}, {}, []
    for key, value in sets.items():
        if key in ACCOUNT_STANDARD:
            ch = _change(key, current.get(key), value)
            changes.append(ch)
            if ch["changed"]:
                payload[key] = value
                checks.append(("std", key, value))
            if key == "name" and ch["changed"] and not _empty(value):
                for hit in reads.search(ctx.client, value.strip(), "sales_account"):
                    if hit.get("type") == "sales_account" and str(hit.get("id")) != str(account_id) \
                            and (hit.get("name") or "").strip().lower() == value.strip().lower():
                        errors.append(f"name: another account is already called {hit.get('name')!r} (id {hit.get('id')})")
        elif key.startswith("cf_"):
            _custom_update(ctx, "account", "sales_accounts", key, value, current["custom_field"],
                           errors, changes, custom, checks)
        elif key in ("owner", "owner_id"):
            _owner_update(ctx, "account", "sales_accounts", value, current, errors, changes, payload, checks)
        else:
            errors.append(f"{key}: not a supported account field. Supported: "
                          f"{list(ACCOUNT_STANDARD) + ['owner', 'cf_*']}")
    if custom:
        payload["custom_field"] = custom
    return {
        "op": "update-account", "entity": "account", "id": account_id, "label": current.get("name"),
        "changes": changes, "errors": errors, "warnings": [],
        "request": {"method": "PUT", "path": f"sales_accounts/{account_id}",
                    "body": {"sales_account": payload}} if payload else None,
        "_checks": checks,
    }


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


ACCOUNT_STANDARD = ("name", "website", "phone", "address", "city", "state", "zipcode", "country")

# What a write is asked to send back (?include=) so it can be checked without a second call.
WRITE_INCLUDES = {"update-contact": "sales_accounts,owner",
                  "update-account": "owner",
                  "update-deal": "sales_account,deal_stage,owner,contacts",
                  "link-deal-contacts": "sales_account,deal_stage,owner,contacts"}


def _record_has(response, plan, keys):
    rec = (response or {}).get({"deal": "deal", "account": "sales_account"}.get(plan["entity"], "contact")) or {}
    return all(k in rec for k in keys)


def _after(ctx, plan, response):
    """The saved record, taken from the write's own response. A GET happens only when the response
    is missing something the checks need, so a verified write normally costs one call, not two."""
    need = []
    if any(c[0] == "cf" for c in plan["_checks"]):
        need.append("custom_field")
    if any(c[0] == "emails" for c in plan["_checks"]):
        need.append("emails")
    deal = plan["entity"] == "deal"
    account = plan["entity"] == "account"
    after = (reads.deal_from if deal else reads.account_from if account else reads.contact_from)(response or {})
    complete = after is not None and _record_has(response, plan, need)
    if plan["op"] == "link-deal-contacts":
        rec = (response or {}).get("deal") or {}
        complete = complete and ("contacts" in (response or {}) or "contacts" in rec)
    if complete:
        return after
    if account:
        return reads.account_from(ctx.client.get(f"sales_accounts/{plan['id']}", params={"include": "owner"}))
    return (reads.get_deal if deal else reads.get_contact)(ctx.client, plan["id"])


def _verify(ctx, plan, response):
    rows = []
    checks = plan["_checks"]
    if plan["op"] == "add-note":
        note = (response or {}).get("note") or {}
        intended = checks[0][2]
        if note.get("id") is not None and note.get("description") is not None:
            # The POST echoes the saved note, so it costs nothing extra to check it.
            actual = note["description"]
        else:
            path, _, key = NOTE_TARGETS[plan["entity"]]
            data = ctx.client.get(f"{path}/{plan['id']}", params={"include": "notes"})
            notes = reads._included(data, data.get(key) or {}, "note", "notes")
            found = next((n for n in notes if str(n.get("id")) == str(note.get("id"))), None)
            actual = found.get("description") if found else None
        rows.append({"field": "note", "intended": intended, "actual": actual,
                     "ok": actual is not None and _strip_html(actual) == intended.strip()})
        return rows

    after = _after(ctx, plan, response)
    if plan["op"] == "link-deal-contacts":
        actual = [c["id"] for c in after["contacts"]]
        want = checks[0][2]
        return [{"field": "contacts", "intended": want, "actual": actual,
                 "ok": values.compare("exact", want, actual)}]

    for chk in checks:
        kind, field, intended = chk[:3]
        if kind in ("std", "cf"):
            actual = after.get(field) if kind == "std" else after["custom_field"].get(field)
            ok = values.compare(chk[3], chk[4], actual) if len(chk) > 3 else same(field, actual, intended)
            rows.append({"field": field, "intended": intended, "actual": actual, "ok": ok})
        elif kind == "emails":
            actual = [e["value"] for e in after.get("emails") or []]
            primary = next((e["value"] for e in after.get("emails") or [] if e.get("is_primary")), None)
            kept = all(old.lower() in {a.lower() for a in actual} for old in chk[3])
            rows.append({"field": "email", "intended": f"{intended} (primary), old addresses kept",
                         "actual": [f"{e['value']}{' (primary)' if e.get('is_primary') else ''}"
                                    for e in after.get("emails") or []],
                         "ok": (primary or "").lower() == intended.lower() and kept})
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
    include = WRITE_INCLUDES.get(plan["op"])
    params = {"include": include} if include else None
    if req["method"] == "PUT":
        response = ctx.client.put(req["path"], req["body"], params=params)
    else:
        response = ctx.client.post(req["path"], req["body"], params=params)
    rows = _verify(ctx, plan, response)
    return {"status": "verified" if all(r["ok"] for r in rows) else "MISMATCH",
            "verification": rows}


def public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}
