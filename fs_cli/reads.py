"""Read-only Freshsales operations."""
from .client import FSError, NotFound

CONTACT_FIELDS = ("id", "display_name", "first_name", "last_name", "email", "job_title",
                  "mobile_number", "work_number", "linkedin", "city", "country", "address", "state",
                  "zipcode", "owner_id")
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

CONTACT_INCLUDE = "sales_accounts,owner,deals,notes"   # one string everywhere so repeat reads share one call
LOOKUP_CHUNK = 25   # emails per search request


def _note_ref(n):
    return {"id": n.get("id"), "description": str(n.get("description") or "")[:200]}


def contact_from(data):
    """Turn a contact response (a GET, or a PUT/POST made with ?include=) into the summary dict."""
    contact = data.get("contact") or {}
    if not contact:
        return None
    accounts = _included(data, contact, "sales_account", "sales_accounts")
    out = {k: contact.get(k) for k in CONTACT_FIELDS}
    out["emails"] = [{"id": e.get("id"), "value": e.get("value"), "is_primary": e.get("is_primary")}
                     for e in contact.get("emails") or [] if isinstance(e, dict)]
    out["custom_field"] = contact.get("custom_field") or {}
    out["subscription_types"] = contact.get("subscription_types")
    out["sales_accounts"] = [_account_ref(a) for a in accounts]
    out["deals"] = [{"id": d.get("id"), "name": d.get("name"), "deal_stage_id": d.get("deal_stage_id")}
                    for d in _included(data, contact, "deal", "deals")]
    out["notes"] = [_note_ref(n) for n in _included(data, contact, "note", "notes")]
    return out


def get_contact(client, contact_id):
    """Return the contact summary dict (raises NotFound). One API call."""
    data = client.get(f"contacts/{contact_id}", params={"include": CONTACT_INCLUDE})
    out = contact_from(data)
    if out is None:
        raise FSError(f"contacts/{contact_id}: empty response")
    return out


def _brief(row):
    return {"id": row.get("id"), "display_name": row.get("display_name"), "email": row.get("email"),
            "job_title": row.get("job_title"), "city": row.get("city")}


def contacts_by_emails(client, emails):
    """Contacts matching any of the emails, found with ONE search per 25 emails instead of one per email.

    Returns {lowercased email: [brief contact, ...]} (an email with no match is simply absent). Falls back
    to one lookup per email if the combined search fails. Search can lag records created minutes ago.
    """
    found = {}
    wanted = [e.strip() for e in emails if e and e.strip()]
    for i in range(0, len(wanted), LOOKUP_CHUNK):
        chunk = wanted[i:i + LOOKUP_CHUNK]
        try:
            data = client.request("POST", "filtered_search/contact", params={"per_page": SEARCH_CAP}, read=True,
                                  body={"filter_rule": [{"attribute": "contact_email.email", "operator": "is_in",
                                                         "value": ",".join(chunk)}]})
            rows = [r for r in data.get("contacts") or [] if isinstance(r, dict)]
            if (data.get("meta") or {}).get("total", len(rows)) > len(rows):
                raise FSError("more matches than one page")
        except FSError:
            for email in chunk:  # slower, but exact for each email
                for hit in contacts_by_email(client, email):
                    found.setdefault(email.lower(), []).append(hit)
            continue
        for row in rows:
            addresses = {str(e.get("value")).lower() for e in row.get("emails") or [] if isinstance(e, dict)}
            addresses.add(str(row.get("email") or "").lower())
            for email in chunk:
                if email.lower() in addresses:
                    found.setdefault(email.lower(), []).append(_brief(row))
    return found


def lookup(client, emails, brief=False):
    """Contacts by email. `brief=True` stops after the search (no custom fields, no per-contact call)."""
    found = contacts_by_emails(client, emails)
    results = []
    for email in emails:
        rows = found.get(email.strip().lower(), [])
        if not rows:
            results.append({"email": email, "status": "not found"})
            continue
        try:
            contacts = rows if brief else [get_contact(client, r["id"]) for r in rows]
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

DEAL_INCLUDE = "sales_account,deal_stage,owner,contacts,notes"   # one string everywhere: one call per deal


def deal_from(data):
    """Turn a deal response (a GET, or a PUT/POST made with ?include=) into the summary dict."""
    deal = data.get("deal") or {}
    if not deal:
        return None
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
        "owner_id": deal.get("owner_id"),
        "sales_account_id": deal.get("sales_account_id"),
        "stage": stage.get("name") if stage else None,
        "expected_close": deal.get("expected_close"),
        "probability": deal.get("probability"),
        "custom_field": deal.get("custom_field") or {},
        "sales_account": account,
        "contacts": [{"id": c.get("id"), "display_name": c.get("display_name"), "email": c.get("email")}
                     for c in _included(data, deal, "contact", "contacts")],
        "notes": [_note_ref(n) for n in _included(data, deal, "note", "notes")],
    }


def get_deal(client, deal_id):
    """Return the deal summary dict with its contacts and notes. One API call."""
    out = deal_from(client.get(f"deals/{deal_id}", params={"include": DEAL_INCLUDE}))
    if out is None:
        raise FSError(f"deals/{deal_id}: empty response")
    return out


def deal_contacts(client, deal_id):
    """Contacts linked to a deal: [{id, display_name}, ...] (same single call as get_deal)."""
    return [{"id": c["id"], "display_name": c["display_name"]} for c in get_deal(client, deal_id)["contacts"]]


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


def find_deals(client, terms, details=False, max_details=10):
    """Search deals and accounts by name. ONE call per term and nothing more by default.

    A search returns up to 100 hits, and fetching each one in full would cost a call apiece, so the hits
    come back as {id, name} only. `details=True` fetches stage, amount and custom fields for the first
    `max_details` deals and accounts of each term and says how many were left out.
    """
    results = []
    deal_cache, acc_cache = {}, {}
    for term in terms:
        try:
            hits = search(client, term, "deal,sales_account")
            deal_hits = [h for h in hits if h.get("type") == "deal"]
            acc_hits = [h for h in hits if h.get("type") == "sales_account"]
            entry = {"term": term}
            if not details:
                entry["deals"] = [{"id": h.get("id"), "name": h.get("name")} for h in deal_hits]
                entry["sales_accounts"] = [{"id": h.get("id"), "name": h.get("name")} for h in acc_hits]
                entry["details"] = "not fetched (use --details); each would cost one API call"
            else:
                for key, group, fetch, cache in (("deals", deal_hits, get_deal, deal_cache),
                                                 ("sales_accounts", acc_hits, get_account_basic, acc_cache)):
                    rows = []
                    for hit in group[:max_details]:
                        if hit.get("id") not in cache:
                            cache[hit.get("id")] = fetch(client, hit.get("id"))
                        rows.append(cache[hit.get("id")])
                    entry[key] = rows
                    if len(group) > max_details:
                        entry[f"{key}_not_detailed"] = [{"id": h.get("id"), "name": h.get("name")}
                                                        for h in group[max_details:]]
            results.append(entry)
        except FSError as exc:
            results.append({"term": term, "error": str(exc)})
    return results


ACCOUNT_INCLUDE = "contacts,deals,owner"   # one string everywhere: one call per account
ACCOUNT_FIELDS = ("id", "name", "website", "phone", "address", "city", "state", "zipcode", "country", "owner_id")


def account_from(data):
    """Summary of an account from a GET, or from a PUT/POST made with ?include=. None if there is no record."""
    acc = data.get("sales_account") or {}
    if not acc:
        return None
    out = {k: acc.get(k) for k in ACCOUNT_FIELDS}
    out["custom_field"] = acc.get("custom_field") or {}
    return out


def get_account(client, account_id):
    data = client.get(f"sales_accounts/{account_id}", params={"include": ACCOUNT_INCLUDE})
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


# ---------- finding records ----------

VIEW_ENTITIES = {"contacts": ("contacts", "contacts"), "deals": ("deals", "deals"),
                 "accounts": ("sales_accounts", "sales_accounts")}  # name -> (api path, response key)
VIEW_DEFAULT_FIELDS = {
    "contacts": ("id", "display_name", "email", "job_title", "city", "cf_contact_type_newct", "cf_rereachout",
                 "cf_reachout_priority", "cf_reachout_final_statustargets"),
    "deals": ("id", "name", "amount", "deal_stage_id", "owner_id"),
    "accounts": ("id", "name", "city", "country", "owner_id"),
}
SEARCH_CAP = 100  # most rows one search request returns


def list_views(client, entity):
    """Saved views for contacts, deals or accounts: [{id, name, is_default, is_public}]."""
    path, _ = VIEW_ENTITIES[entity]
    data = client.get(f"{path}/filters", meta=True)
    return [{"id": f.get("id"), "name": f.get("name"), "is_default": f.get("is_default"),
             "is_public": f.get("is_public")} for f in data.get("filters") or []]


def _value(row, field):
    if field.startswith("cf_"):
        return (row.get("custom_field") or {}).get(field)
    return row.get(field)


def parse_where(items):
    """['city~Reading,Oxford', 'cf_contact_type_newct=SO-CF'] -> [(field, op, [values])].

    `=` matches a whole value, `~` matches part of it. Both ignore case and accept
    comma-separated alternatives (any one matching is enough).
    """
    conds = []
    for item in items or []:
        for op in ("~", "="):
            if op in item:
                field, _, raw = item.partition(op)
                break
        else:
            raise FSError(f"--where expects field=value or field~value, got {item!r}")
        vals = [v.strip().lower() for v in raw.split(",") if v.strip()]
        if not field.strip() or not vals:
            raise FSError(f"--where {item!r}: needs a field and at least one value")
        conds.append((field.strip(), op, vals))
    return conds


def row_matches(row, conds):
    for field, op, vals in conds:
        if field == "domain":
            doms = {str(e).rsplit("@", 1)[-1].lower() for e in
                    [row.get("email")] + [x.get("value") for x in row.get("emails") or [] if isinstance(x, dict)]
                    if e and "@" in str(e)}
            if not any(v in doms for v in vals):
                return False
            continue
        have = _value(row, field)
        text = ("" if have is None else str(have)).strip().lower()
        ok = any(v == text for v in vals) if op == "=" else any(v in text for v in vals)
        if not ok:
            return False
    return True


BUDGET_RESERVE = 100   # calls to leave in the account's window when a sweep decides whether to carry on


def view_rows(client, entity, view_id, where=None, max_pages=10, stop_after=None, per_page=100,
              reserve=BUDGET_RESERVE):
    """Read a saved view page by page and keep the whole records that match `where`.

    Saved views return whole records including custom fields, so they can be filtered on anything.
    Each page is one API request (100 rows) out of the account's 1000 an hour, so a sweep stops when it
    has `stop_after` matches, when it has read `max_pages`, or when Freshsales reports fewer than `reserve`
    calls left. Returns (rows, info) where info says how much was read and why it stopped.
    """
    path, key = VIEW_ENTITIES[entity]
    conds = parse_where(where)
    rows, page, pages_read, total, total_pages, stopped = [], 1, 0, None, None, None
    while page <= max_pages:
        left = client.remaining
        if pages_read and left is not None and left <= reserve:
            stopped = f"only {left} API calls left in the account's window"
            break
        data = client.get(f"{path}/view/{view_id}", params={"page": page, "per_page": per_page})
        pages_read += 1
        meta = data.get("meta") or {}
        total, total_pages = meta.get("total", total), meta.get("total_pages", total_pages)
        batch = [r for r in data.get(key) or [] if isinstance(r, dict)]
        rows.extend(r for r in batch if row_matches(r, conds))
        if stop_after and len(rows) >= stop_after:
            rows = rows[:stop_after]
            stopped = f"found the {stop_after} matches asked for"
            break
        if not batch or (total_pages is not None and page >= total_pages):
            break
        page += 1
    truncated = total_pages is not None and pages_read < total_pages
    if truncated and not stopped:
        stopped = f"reached --max-pages {max_pages}"
    return rows, {"rows_in_view": total, "pages_read": pages_read, "pages_in_view": total_pages,
                  "truncated": truncated, "stopped": stopped if truncated else None}


def list_view(client, entity, view_id, where=None, fields=None, max_pages=10, per_page=100, stop_after=None):
    cols = list(fields or VIEW_DEFAULT_FIELDS[entity])
    rows, info = view_rows(client, entity, view_id, where, max_pages, stop_after, per_page)
    return {"entity": entity, "view_id": view_id, **info, "matched": len(rows),
            "rows": [{c: _value(r, c) for c in cols} for r in rows]}


def _rule(attr, op, value):
    return {"attribute": attr, "operator": op, "value": value}


def find_contacts(client, name=None, email=None, city=None, ctype=None, domain=None):
    """Search contacts. Server-side filters are ANDed. Returns {total, shown_rows, rows, notes}.

    Filtered search matches exactly (names also allow a partial surname) ignoring case, accepts
    comma-separated alternatives, and returns no custom fields. `domain` uses free-text search, which
    stops at 100 results, so it must be given alone.
    """
    notes = []
    if domain:
        if any([name, email, city, ctype]):
            raise FSError("--domain uses free-text search and can't be combined with other filters")
        want = domain.strip().lower().lstrip("@")
        hits = [h for h in search(client, want, "contact") if h.get("type") == "contact"]
        rows = [{"id": h.get("id"), "display_name": h.get("name"), "email": h.get("email")}
                for h in hits if str(h.get("email") or "").lower().endswith("@" + want)]
        if len(hits) >= SEARCH_CAP:
            notes.append(f"search stops at {SEARCH_CAP} results, so some {want} contacts may be missing; "
                         "use list-view with --where domain=... for a complete list")
        return {"total": len(rows), "rows": rows, "notes": notes}
    rules = []
    if name:
        parts = name.split()
        if len(parts) == 1:
            rules.append(_rule("last_name", "contains", parts[0]))
        else:
            rules += [_rule("first_name", "is_in", parts[0]), _rule("last_name", "is_in", " ".join(parts[1:]))]
    if email:
        rules.append(_rule("contact_email.email", "is_in", email))
    if city:
        rules.append(_rule("city", "is_in", city))
    if ctype:
        valid = picklist_map(client, "contacts").get("cf_contact_type_newct") or []
        bad = [t.strip() for t in ctype.split(",") if t.strip() and t.strip() not in valid]
        if bad:
            raise FSError(f"cf_contact_type_newct: {bad} not valid. Valid: {valid}")
        rules.append(_rule("cf_contact_type_newct", "is_in", ctype))
    if not rules:
        raise FSError("find-contacts needs at least one of --name, --email, --city, --type or --domain")
    data = client.request("POST", "filtered_search/contact", params={"per_page": SEARCH_CAP}, read=True,
                          body={"filter_rule": rules})
    rows = [r for r in data.get("contacts") or [] if isinstance(r, dict)]
    total = (data.get("meta") or {}).get("total", len(rows))
    if total > len(rows):
        notes.append(f"{total} contacts match but only {len(rows)} were returned; narrow the filters")
    return {"total": total, "rows": rows, "notes": notes}


# ---------- fields / picklists ----------

def get_fields(client, entity):
    data = client.get(f"settings/{entity}/fields", meta=True)
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
    data = client.get("selector/deal_stages", meta=True)
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
