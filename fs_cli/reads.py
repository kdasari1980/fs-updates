"""Read-only Freshsales operations."""
from .client import FSError, NotFound

CONTACT_FIELDS = ("id", "display_name", "first_name", "last_name", "email", "job_title",
                  "mobile_number", "linkedin", "city")
PICKLIST_FIELDS = ("cf_rereachout", "cf_contact_type_newct", "cf_reachout_final_statustargets")
FIELD_ENTITIES = ("contacts", "sales_accounts", "deals")


def _included(data, record, singular, plural, wanted_id=None):
    """Find included records wherever Freshsales put them.

    Included associations may sit inside the record or at the top level of
    the response, and may be singular or plural; the docs do not pin this down.
    """
    candidates = []
    for container in (record, data):
        for key in (plural, singular):
            value = container.get(key)
            if isinstance(value, list):
                candidates.extend(v for v in value if isinstance(v, dict))
            elif isinstance(value, dict):
                candidates.append(value)
    if wanted_id is None:
        seen, out = set(), []
        for c in candidates:
            if c.get("id") not in seen:
                seen.add(c.get("id"))
                out.append(c)
        return out
    for c in candidates:
        if str(c.get("id")) == str(wanted_id):
            return c
    return None


def _account_ref(acc):
    ref = {"id": acc.get("id"), "name": acc.get("name")}
    if "is_primary" in acc:
        ref["is_primary"] = acc.get("is_primary")
    return ref


# ---------- contacts ----------

def get_contact(client, contact_id):
    """Return the contact summary dict (raises NotFound)."""
    data = client.get(f"contacts/{contact_id}", params={"include": "sales_accounts"})
    contact = data.get("contact") or {}
    if not contact:
        raise FSError(f"contacts/{contact_id}: empty response")
    accounts = _included(data, contact, "sales_account", "sales_accounts")
    out = {k: contact.get(k) for k in CONTACT_FIELDS}
    out["custom_field"] = contact.get("custom_field") or {}
    out["sales_accounts"] = [_account_ref(a) for a in accounts]
    return out


def lookup(client, emails):
    results = []
    for email in emails:
        try:
            data = client.get("lookup", params={"q": email, "f": "email", "entities": "contact"})
            matches = (data.get("contacts") or {}).get("contacts") or []
            if not matches:
                results.append({"email": email, "status": "not found"})
                continue
            contacts = [get_contact(client, m["id"]) for m in matches]
            results.append({"email": email, "status": "found", "contacts": contacts})
        except FSError as exc:
            results.append({"email": email, "status": "error", "error": str(exc)})
    return results


def contacts_by_email(client, email):
    """Contacts whose email matches exactly: [{id, display_name}, ...]. Search lags new records by minutes."""
    data = client.get("lookup", params={"q": email, "f": "email", "entities": "contact"})
    return [{"id": m.get("id"), "display_name": m.get("display_name")}
            for m in (data.get("contacts") or {}).get("contacts") or []]


# ---------- deals / accounts ----------

def get_deal(client, deal_id):
    data = client.get(f"deals/{deal_id}", params={"include": "sales_account,deal_stage"})
    deal = data.get("deal") or {}
    if not deal:
        raise FSError(f"deals/{deal_id}: empty response")
    stage = _included(data, deal, "deal_stage", "deal_stages", deal.get("deal_stage_id"))
    account = None
    if deal.get("sales_account_id") is not None:
        acc = _included(data, deal, "sales_account", "sales_accounts", deal.get("sales_account_id"))
        account = _account_ref(acc) if acc else {"id": deal.get("sales_account_id"), "name": None}
    return {
        "id": deal.get("id"),
        "name": deal.get("name"),
        "amount": deal.get("amount"),
        "deal_stage_id": deal.get("deal_stage_id"),
        "deal_pipeline_id": deal.get("deal_pipeline_id"),
        "stage": stage.get("name") if stage else None,
        "expected_close": deal.get("expected_close"),
        "probability": deal.get("probability"),
        "custom_field": deal.get("custom_field") or {},
        "sales_account": account,
    }


def get_account_basic(client, account_id):
    data = client.get(f"sales_accounts/{account_id}")
    acc = data.get("sales_account") or {}
    return {
        "id": acc.get("id"),
        "name": acc.get("name"),
        "website": acc.get("website"),
        "city": acc.get("city"),
        "custom_field": acc.get("custom_field") or {},
    }


def search(client, term, include):
    data = client.get("search", params={"q": term, "include": include, "per_page": 100})
    if isinstance(data, dict):  # defensive: docs show a bare array
        data = data.get("results") or []
    return [r for r in data if isinstance(r, dict)]


def find_deals(client, terms):
    results = []
    deal_cache, acc_cache = {}, {}
    for term in terms:
        try:
            hits = search(client, term, "deal,sales_account")
            deals, accounts = [], []
            for hit in hits:
                hid, htype = hit.get("id"), hit.get("type")
                if htype == "deal":
                    if hid not in deal_cache:
                        deal_cache[hid] = get_deal(client, hid)
                    deals.append(deal_cache[hid])
                elif htype == "sales_account":
                    if hid not in acc_cache:
                        acc_cache[hid] = get_account_basic(client, hid)
                    accounts.append(acc_cache[hid])
            results.append({"term": term, "deals": deals, "sales_accounts": accounts})
        except FSError as exc:
            results.append({"term": term, "error": str(exc)})
    return results


def get_account(client, account_id):
    data = client.get(f"sales_accounts/{account_id}", params={"include": "contacts,deals"})
    acc = data.get("sales_account") or {}
    if not acc:
        raise FSError(f"sales_accounts/{account_id}: empty response")
    contacts = _included(data, acc, "contact", "contacts")
    deals = _included(data, acc, "deal", "deals")
    account = {k: v for k, v in acc.items() if k not in ("contacts", "deals")}
    return {
        "account": account,
        "contacts": [{"id": c.get("id"), "display_name": c.get("display_name"),
                      "email": c.get("email"), "job_title": c.get("job_title")} for c in contacts],
        "deals": [{"id": d.get("id"), "name": d.get("name"), "amount": d.get("amount"),
                   "deal_stage_id": d.get("deal_stage_id")} for d in deals],
    }


# ---------- fields / picklists ----------

def get_fields(client, entity):
    data = client.get(f"settings/{entity}/fields")
    return data.get("fields") or []


def choice_values(field):
    return [c.get("value") for c in (field.get("choices") or []) if isinstance(c, dict)]


def picklist_map(client, entity):
    """{field_name: [valid values]} for every field on the entity that has choices."""
    out = {}
    for f in get_fields(client, entity):
        values = choice_values(f)
        if values:
            out[f.get("name")] = values
    return out


def picklists(client, names=PICKLIST_FIELDS):
    remaining = set(names)
    result = {}
    for entity in FIELD_ENTITIES:
        if not remaining:
            break
        for f in get_fields(client, entity):
            name = f.get("name")
            if name in remaining:
                result[name] = {"entity": entity, "label": f.get("label"),
                                "type": f.get("type"), "values": choice_values(f)}
                remaining.discard(name)
    for name in remaining:
        result[name] = {"error": "field not found on contacts, sales_accounts or deals"}
    return {n: result[n] for n in names}


def deal_stages(client):
    data = client.get("selector/deal_stages")
    return data.get("deal_stages") or []


def find_account_by_exact_name(client, name):
    target = name.strip()
    hits = [h for h in search(client, target, "sales_account") if h.get("type") == "sales_account"]
    exact = [h for h in hits if (h.get("name") or "").strip() == target]
    if not exact:
        near = sorted({h.get("name") for h in hits if h.get("name")})[:10]
        hint = f" Close matches: {near}" if near else ""
        raise FSError(f"No sales account named exactly {target!r}.{hint}")
    if len({h.get("id") for h in exact}) > 1:
        raise FSError(f"Multiple sales accounts named {target!r}: ids {[h.get('id') for h in exact]}")
    return {"id": exact[0]["id"], "name": exact[0].get("name")}


__all__ = ["NotFound"]
