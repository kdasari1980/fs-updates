"""Create operations: contacts, deals and accounts.

Same shape as writes.py. A create is first a plan (validated against the live
field metadata and checked for duplicates; nothing is sent), then applied, then
the new record is read back and every intended field is checked.

Behaviour confirmed against the live API with throwaway records:
- dropdown, radio and checkbox custom fields take their text values;
- multi-selects need one ';'-joined string (a list or commas silently drop values);
- owner, lifecycle stage, contact status and currency only appear on a read-back
  when asked for with ?include=;
- new records take a few minutes to show up in search, so the duplicate checks
  cannot see records created moments ago.
"""
import re

from . import checks as rules
from . import reads, values
from .client import FSError, NotFound
from .writes import _change, _empty

WEB_BASE = "https://tern.freshworks.com/crm/sales/"

ENTITIES = {
    "contact": {
        "path": "contacts", "key": "contact", "fields": "contacts", "web": "contacts",
        "text": ("first_name", "last_name", "job_title", "mobile_number", "work_number", "linkedin",
                 "city", "country", "address", "state", "zipcode"),
        "numeric": (), "dates": (),
        "ids": {"owner": "owner_id", "lifecycle_stage": "lifecycle_stage_id",
                "contact_status": "contact_status_id", "lead_source": "lead_source_id",
                "territory": "territory_id"},
        "include": "owner,sales_accounts,lifecycle_stage,contact_status",
    },
    "deal": {
        "path": "deals", "key": "deal", "fields": "deals", "web": "deals",
        "text": ("name",), "numeric": ("amount", "probability"), "dates": ("expected_close",),
        "ids": {"owner": "owner_id", "stage": "deal_stage_id", "deal_stage": "deal_stage_id",
                "pipeline": "deal_pipeline_id", "currency": "currency_id",
                "lead_source": "lead_source_id", "territory": "territory_id"},
        "include": "owner,sales_account,deal_stage,contacts,currency",
    },
    "account": {
        "path": "sales_accounts", "key": "sales_account", "fields": "sales_accounts", "web": "accounts",
        "text": ("name", "website", "phone", "address", "city", "state", "zipcode", "country"),
        "numeric": ("annual_revenue",), "dates": (),
        "ids": {"owner": "owner_id", "industry_type": "industry_type_id",
                "business_type": "business_type_id", "territory": "territory_id"},
        "include": "owner",
    },
}

# Defaults from the fs-update skill. They are shown in the dry-run diff marked
# "default" and validated against the live picklists like any other value, so a
# stale default shows up as INVALID rather than being silently ignored.
PRESETS = {
    "target-contact": ("contact", {
        "owner": "Karthik Dasari", "lifecycle_stage": "Lead", "contact_status": "Unqualified Leads",
        "cf_contact_type_newct": "TG-Owner/Manager", "cf_reachout_final_statustargets": "Unqualified",
        "cf_rereachout": "Chase", "subscription_types": "Deal Announcements",
        "sales_account": "Not Qualified"}),
    "advisor-contact": ("contact", {
        "owner": "Karthik Dasari", "cf_rereachout": "Chase",
        "cf_reachout_final_statustargets": "Unqualified"}),
    "target-deal": ("deal", {
        "amount": "-1", "owner": "Karthik Dasari", "pipeline": "All Tern Deals",
        "stage": "04 Plan-Teaser/Suspect", "currency": "GBP", "cf_deal_currency": "GBP",
        "cf_deal_stagelatest": "10 Suspect", "cf_chaser_status": "Chase", "cf_source_deal": "Direct",
        "cf_status": "Healthy", "cf_deal_type": "Standalone", "cf_bolton_only": "Yes",
        "cf_bolton_platform": "Other"}),
    "advisor-account": ("account", {"owner": "Karthik Dasari"}),
}


def preset_names(entity):
    return sorted(n for n, (e, _) in PRESETS.items() if e == entity)


# ---------- value helpers ----------

def _choice(ctx, entity, field, value, errors):
    return values.choice(ctx, ENTITIES[entity]["fields"], field, value, errors, entity)


def _custom(ctx, entity, field, value, errors):
    return values.custom(ctx, ENTITIES[entity]["fields"], field, value, errors, entity)


def _canonical(entity, key):
    spec = ENTITIES[entity]
    return spec["ids"].get(key, key)


# ---------- plan ----------

def plan_create(ctx, entity, sets, preset=None):
    spec = ENTITIES[entity]
    defaults = {}
    if preset:
        if preset not in PRESETS:
            raise FSError(f"unknown preset {preset!r}. Valid for {entity}: {preset_names(entity)}")
        p_entity, defaults = PRESETS[preset]
        if p_entity != entity:
            raise FSError(f"preset {preset!r} is for a {p_entity}, not a {entity}. "
                          f"Valid for {entity}: {preset_names(entity)}")
    user = {_canonical(entity, k.strip()): v for k, v in (sets or {}).items()}
    merged = {**{_canonical(entity, k): v for k, v in defaults.items()}, **user}
    if not merged:
        raise FSError(f"create-{entity} needs at least one --set key=value (or a --preset)")

    errors, warnings, changes, checks = [], [], [], []
    payload, custom = {}, {}
    id_fields = set(spec["ids"].values())

    def record(field, display, source, intended=None, kind="text", expected=None, where=("rec", None)):
        ch = _change(field, None, display)
        ch["source"] = source
        changes.append(ch)
        checks.append({"label": field, "intended": display if intended is None else intended,
                       "kind": kind, "expected": display if expected is None else expected,
                       "where": where if where[1] else ("rec", field)})

    for key, value in merged.items():
        source = "set" if key in user else "default"
        if key in ("email", "emails") and entity == "contact":
            emails = values.split(value)
            bad = [e for e in emails if not values.EMAIL_RE.match(e)]
            if bad or not emails:
                errors.append(f"emails: not a valid email address: {bad or [value]}")
                continue
            payload["emails"] = [{"value": e, "is_primary": i == 0} for i, e in enumerate(emails)]
            shown = ", ".join(e + (" (primary)" if i == 0 else "") for i, e in enumerate(emails))
            record("emails", shown, source, intended=emails, kind="emails", expected=emails,
                   where=("x", "emails"))
        elif key in spec["text"]:
            text = str(value).strip()
            if _empty(text):
                errors.append(f"{key}: empty value")
                continue
            payload[key] = text
            record(key, text, source, kind="phone" if key in ("mobile_number", "work_number") else "text")
        elif key in spec["numeric"]:
            try:
                num = values.number(value)
            except ValueError:
                errors.append(f"{key}: {value!r} is not a number")
                continue
            if key == "probability" and not 0 <= num <= 100:
                errors.append("probability: must be between 0 and 100")
                continue
            payload[key] = num
            record(key, num, source, kind="num", expected=num)
        elif key in spec["dates"]:
            if not values.DATE_RE.match(str(value).strip()):
                errors.append(f"{key}: {value!r} is not a date (YYYY-MM-DD)")
                continue
            payload[key] = str(value).strip()
            record(key, payload[key], source, kind="date", expected=payload[key])
        elif key in id_fields:
            cid = _choice(ctx, entity, key, value, errors)
            if cid is None:
                continue
            payload[key] = cid
            where = ("rec", key)
            if key in ("lifecycle_stage_id", "contact_status_id", "currency_id"):
                where = ("x", key)
            record(key, f"{str(value).strip()} ({cid})", source, kind="id", expected=cid, where=where)
        elif key == "subscription_types" and entity == "contact":
            ids = []
            for part in values.split(value):
                cid = _choice(ctx, entity, "subscription_types", part, errors)
                if cid is not None:
                    ids.append(str(cid))
            if not ids:
                continue
            payload["subscription_types"] = ";".join(ids)
            record(key, f"{value} ({payload['subscription_types']})", source, kind="set",
                   expected=set(ids), where=("rec", "subscription_types"))
        elif key == "sales_account" and entity in ("contact", "deal"):
            try:
                acc = reads.find_account_by_exact_name(ctx.client, str(value))
            except FSError as exc:
                errors.append(f"sales_account: {exc}")
                continue
            if entity == "contact":
                payload["sales_accounts"] = [{"id": acc["id"], "is_primary": True}]
                record("sales_account", f"{acc['name']} ({acc['id']}, primary)", source, kind="has",
                       expected=acc["id"], where=("x", "sales_account_ids"))
            else:
                payload["sales_account_id"] = acc["id"]
                record("sales_account", f"{acc['name']} ({acc['id']})", source, kind="id",
                       expected=acc["id"], where=("rec", "sales_account_id"))
        elif key == "contacts" and entity == "deal":
            ids = []
            for part in values.split(value):
                if not part.isdigit():
                    errors.append(f"contacts: {part!r} is not a contact id")
                    continue
                try:
                    reads.get_contact(ctx.client, part)
                except NotFound:
                    errors.append(f"contacts: contact {part} does not exist")
                    continue
                except FSError as exc:
                    errors.append(f"contacts: {exc}")
                    continue
                ids.append(int(part))
            if not ids:
                continue
            payload["contacts_added_list"] = ids
            record("contacts", ", ".join(map(str, ids)), source, kind="all",
                   expected=set(ids), where=("x", "contact_ids"))
        elif key.startswith("cf_"):
            res = _custom(ctx, entity, key, value, errors)
            if res is None:
                continue
            pvalue, kind, expected = res
            custom[key] = pvalue
            record(key, pvalue, source, kind=kind, expected=expected, where=("cf", key))
        else:
            supported = (list(spec["text"]) + list(spec["numeric"]) + list(spec["dates"])
                         + sorted(set(spec["ids"])) + ["cf_*"]
                         + {"contact": ["email", "emails", "sales_account", "subscription_types"],
                            "deal": ["sales_account", "contacts"], "account": []}[entity])
            errors.append(f"{key}: not a supported {entity} field. Supported: {supported}")

    if custom:
        payload["custom_field"] = custom

    _check_required(entity, payload, errors)
    if not errors:
        _check_duplicates(ctx, entity, payload, errors, warnings)

    if entity == "contact":
        warnings.extend(m for _, m in rules.contact_issues({
            "custom_field": payload.get("custom_field"), "city": payload.get("city"),
            "linkedin": payload.get("linkedin"), "emails": payload.get("emails")}))
    label = {"contact": " ".join(str(payload.get(k, "")) for k in ("first_name", "last_name")).strip(),
             "deal": payload.get("name"), "account": payload.get("name")}[entity]
    return {
        "op": f"create-{entity}", "entity": entity, "id": None, "label": label or None,
        "changes": changes, "errors": errors, "warnings": warnings,
        "request": {"method": "POST", "path": spec["path"], "body": {spec["key"]: payload}},
        "_checks": checks,
    }


def prefetch_duplicates(ctx, emails):
    """Find existing contacts for every email in a batch with one search per 25 emails, instead of one
    lookup per contact. The per-contact duplicate check then reads from this."""
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()} - ctx.email_checked)
    if not wanted:
        return
    for email, hits in reads.contacts_by_emails(ctx.client, wanted).items():
        ctx.email_hits[email] = hits
    ctx.email_checked.update(wanted)


def _check_required(entity, payload, errors):
    if entity == "contact":
        if "first_name" not in payload:
            errors.append("first_name: required")
        if "emails" not in payload and "mobile_number" not in payload:
            errors.append("a contact needs an email or a mobile_number")
    elif entity == "deal":
        for need in ("name", "amount"):
            if need not in payload:
                errors.append(f"{need}: required (use --preset target-deal for amount -1)")
    elif "name" not in payload:
        errors.append("name: required")


def _check_duplicates(ctx, entity, payload, errors, warnings):
    seen = ctx.seen
    if entity == "contact":
        for item in payload.get("emails", []):
            email = item["value"].lower()
            if email in seen:
                errors.append(f"duplicate: {email} appears more than once in this batch")
                continue
            seen.add(email)
            if email in ctx.email_checked:
                hits = ctx.email_hits.get(email, [])
            else:
                hits = reads.contacts_by_email(ctx.client, item["value"])
            for hit in hits:
                errors.append(f"duplicate: {item['value']} already exists as contact {hit.get('id')} "
                              f"({hit.get('display_name')}). Use update-contact instead")
        return
    name = payload.get("name")
    if not name:
        return
    key = (entity, name.strip().lower())
    if key in seen:
        errors.append(f"duplicate: {name!r} appears more than once in this batch")
        return
    seen.add(key)
    kind = "deal" if entity == "deal" else "sales_account"
    noun = "deal" if entity == "deal" else "account"
    article = "a deal" if entity == "deal" else "an account"
    lowered = name.strip().lower()
    hits = {}
    for term in _search_terms(name):
        for h in reads.search(ctx.client, term, kind):
            if h.get("type") == kind:
                hits[h.get("id")] = h
    exact = [h for h in hits.values() if (h.get("name") or "").strip().lower() == lowered]
    for h in exact:
        errors.append(f"duplicate: {article} named {h.get('name')!r} already exists (id {h.get('id')})")
    near = sorted(f"{h.get('name')} ({h.get('id')})" for h in hits.values() if h not in exact)
    if near:
        warnings.append(f"similar {noun}s already exist (check these are different): {near[:8]}")


LEGAL_SUFFIXES = {"ltd", "limited", "plc", "llp", "llc", "inc", "group", "holdings", "uk", "the"}


def _search_terms(name):
    """The full name, plus the name without trailing legal/group words, so look-alikes are found."""
    words = name.strip().split()
    terms = [name.strip()]
    stem = list(words)
    while len(stem) > 1 and stem[-1].lower().strip(".,") in LEGAL_SUFFIXES:
        stem.pop()
    if stem and len(stem) < len(words):
        terms.append(" ".join(stem))
    return terms


# ---------- apply + verify ----------

def _first_id(data, *keys):
    for k in keys:
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0].get("id")
        if isinstance(v, dict):
            return v.get("id")
    return None


def _extract_after(entity, data):
    """The saved record and its included extras, from a GET or from a POST/PUT made with ?include=."""
    spec = ENTITIES[entity]
    rec = data.get(spec["key"]) or {}
    if not rec:
        return None
    x = {}
    if entity == "contact":
        x["sales_account_ids"] = [a.get("id") for a in
                                  reads._included(data, rec, "sales_account", "sales_accounts")]
        x["lifecycle_stage_id"] = _first_id(data, "lifecycle_stages", "lifecycle_stage")
        x["contact_status_id"] = _first_id(data, "contact_status", "contact_statuses")
        x["emails"] = [e.get("value") for e in rec.get("emails") or [] if isinstance(e, dict)]
    elif entity == "deal":
        contacts = reads._included(data, rec, "contact", "contacts")
        x["contact_ids"] = [c.get("id") for c in contacts]
        x["currency_id"] = _first_id(data, "currencies", "currency")
    return {"rec": rec, "cf": rec.get("custom_field") or {}, "x": x}


def _complete(data, after, checks):
    """True when the response has everything the checks look at, so no second call is needed."""
    if after is None:
        return False
    rec = after["rec"]
    for chk in checks:
        where, field = chk["where"]
        if where == "cf" and "custom_field" not in rec:
            return False
        if where == "x":
            if field in ("lifecycle_stage_id", "contact_status_id", "currency_id") and after["x"].get(field) is None:
                return False
            if field == "emails" and "emails" not in rec:
                return False
            if field == "sales_account_ids" and "sales_accounts" not in rec and "sales_accounts" not in data:
                return False
            if field == "contact_ids" and "contacts" not in rec and "contacts" not in data:
                return False
    return True


def _fetch_after(ctx, entity, new_id):
    """Fallback: read the new record back (one extra call)."""
    spec = ENTITIES[entity]
    data = ctx.client.get(f"{spec['path']}/{new_id}", params={"include": spec["include"]})
    after = _extract_after(entity, data)
    if after is None:
        raise FSError(f"{spec['path']}/{new_id}: empty response on read-back")
    return after


def apply_create(ctx, plan):
    req = plan["request"]
    entity = plan["entity"]
    spec = ENTITIES[entity]
    # Ask for the extras back on the POST itself: the response is the saved record, so it can be
    # verified without a second call.
    response = ctx.client.post(req["path"], req["body"], params={"include": spec["include"]})
    new_id = (response.get(spec["key"]) or {}).get("id")
    if new_id is None:
        raise FSError(f"POST {req['path']}: response had no {spec['key']} id; check Freshsales before retrying")
    result = {"created_id": new_id, "url": f"{WEB_BASE}{spec['web']}/{new_id}"}
    after = _extract_after(entity, response)
    if not _complete(response, after, plan["_checks"]):
        try:
            after = _fetch_after(ctx, entity, new_id)
        except FSError as exc:
            return {**result, "status": "CREATED-UNVERIFIED", "error": str(exc), "verification": []}
    rows = []
    for chk in plan["_checks"]:
        where, field = chk["where"]
        actual = after[where].get(field)
        rows.append({"field": chk["label"], "intended": chk["intended"],
                     "actual": actual, "ok": values.compare(chk["kind"], chk["expected"], actual)})
    status = "verified" if all(r["ok"] for r in rows) else "MISMATCH"
    return {**result, "status": status, "verification": rows}
