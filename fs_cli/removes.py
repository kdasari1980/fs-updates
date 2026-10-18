"""Delete and merge. Both are permanent, so both are dry-run first, guarded, and report what went.

Delete guards (set by Karthik):
- a contact is not deleted if it is attached to a live deal, or has notes;
- a deal is not deleted if it has notes;
- an account is not deleted while it still has contacts or deals.
There is no "forget" (hard delete) and never `delete_associated_contacts_deals`. A contact that is blocked can be
unlinked from its deal first with `link-deal-contacts`, which is the explicit way round the live-deal guard.

Each delete costs one read (the record, with its deals and notes included) and one DELETE. The DELETE
answers `true`, which is what is checked, so there is no read-back.
"""
from . import reads, values
from .client import FSError, NotFound
from .writes import _change

# Stage numbers of deals that are over: Screened Out, Rejected by Tern (to notify / notified),
# Rejected by Seller, Sold to Other, Sold to Tern. Every other stage counts as live, including the
# "Future" and "No Response" ones, because those can still come back.
DEAD_STAGE_CODES = ("18", "64", "68", "72", "76", "80")


def is_live_stage(name):
    """A stage whose name we couldn't read counts as live: the guard errs on the side of keeping the record."""
    return not (name and name.split(" ", 1)[0] in DEAD_STAGE_CODES)


def _stage_names(ctx):
    return {s.get("id"): s.get("name") for s in reads.deal_stages(ctx.client)}


def _deal_line(d, stages):
    stage = stages.get(d.get("deal_stage_id")) or d.get("stage")
    return f"{d.get('name')} (id {d.get('id')}, {stage or 'stage unknown'})"


def plan_delete(ctx, entity, record_id):
    errors, warnings, changes = [], [], []
    if entity == "contact":
        rec = reads.get_contact(ctx.client, record_id)
        stages = _stage_names(ctx)
        label = rec.get("display_name")
        live = []
        for d in rec["deals"]:
            if d.get("deal_stage_id") is None:  # the contact's deal list didn't say: ask the deal itself
                full = reads.get_deal(ctx.client, d["id"])
                d = {**d, "deal_stage_id": full.get("deal_stage_id"), "name": d.get("name") or full.get("name")}
            name = stages.get(d.get("deal_stage_id"))
            changes.append(_change("deal", f"{_deal_line(d, stages)}{'' if not is_live_stage(name) else '  LIVE'}",
                                   "(contact unlinked)", None, None) | {"changed": False, "overwrites_existing": False})
            if is_live_stage(name):
                live.append(d)
        if live:
            errors.append(f"contact {record_id} ({label}) is attached to {len(live)} live deal(s): "
                          + "; ".join(_deal_line(d, stages) for d in live)
                          + f". Unlink first with: link-deal-contacts {live[0]['id']} --remove {record_id}")
        if rec["notes"]:
            errors.append(f"contact {record_id} ({label}) has {len(rec['notes'])} note(s), e.g. "
                          f"{rec['notes'][0]['description'][:80]!r}. Notes are history: not deleted")
        ident = f"{label}" + (f" <{rec.get('email')}>" if rec.get("email") else "")
        snapshot = {k: rec.get(k) for k in ("id", "display_name", "email", "emails", "job_title", "city",
                                            "owner_id", "sales_accounts", "deals", "notes", "custom_field")}
        path = f"contacts/{record_id}"
    elif entity == "deal":
        rec = reads.get_deal(ctx.client, record_id)
        label = rec.get("name")
        for c in rec["contacts"]:
            changes.append(_change("contact", f"{c.get('display_name')} (id {c.get('id')}) linked", "(unlinked, kept)",
                                   None, None) | {"changed": False, "overwrites_existing": False})
        if rec["notes"]:
            errors.append(f"deal {record_id} ({label}) has {len(rec['notes'])} note(s), e.g. "
                          f"{rec['notes'][0]['description'][:80]!r}. Notes are history: not deleted")
        if rec["contacts"]:
            warnings.append(f"{len(rec['contacts'])} contact(s) stay in Freshsales; they are only unlinked from the deal")
        ident = f"{label} (stage {rec.get('stage') or '?'}, amount {rec.get('amount')})"
        snapshot = {k: rec.get(k) for k in ("id", "name", "amount", "stage", "deal_stage_id", "owner_id",
                                            "sales_account", "contacts", "notes", "custom_field")}
        path = f"deals/{record_id}"
    else:
        rec = reads.get_account(ctx.client, record_id)
        acc = rec["account"]
        label = acc.get("name")
        for c in rec["contacts"]:
            changes.append(_change("contact", f"{c.get('display_name')} (id {c.get('id')})", None, None, None)
                           | {"changed": False, "overwrites_existing": False})
        for d in rec["deals"]:
            changes.append(_change("deal", f"{d.get('name')} (id {d.get('id')})", None, None, None)
                           | {"changed": False, "overwrites_existing": False})
        if rec["contacts"] or rec["deals"]:
            errors.append(f"account {record_id} ({label}) still has {len(rec['contacts'])} contact(s) and "
                          f"{len(rec['deals'])} deal(s). Move or remove them first")
        ident = f"{label}"
        snapshot = {"id": acc.get("id"), "name": acc.get("name"), "website": acc.get("website"),
                    "city": acc.get("city"), "contacts": rec["contacts"], "deals": rec["deals"]}
        path = f"sales_accounts/{record_id}"

    head = _change("record", f"{ident} (id {record_id})", "(DELETED)", None, None)
    head.update({"changed": True, "overwrites_existing": True, "flag": "DELETE"})
    changes.insert(0, head)
    return {"op": f"delete-{entity}", "entity": entity, "id": record_id, "label": label, "changes": changes,
            "errors": errors, "warnings": warnings, "snapshot": snapshot,
            "request": {"method": "DELETE", "path": path, "body": None}, "_checks": []}


def apply_delete(ctx, plan):
    """Send the DELETE and trust its `true`. Returns what was deleted, because it can't be recovered."""
    req = plan["request"]
    answer = ctx.client.delete(req["path"])
    ok = answer is True
    return {"status": "deleted" if ok else "MISMATCH", "deleted": plan["snapshot"],
            "verification": [{"field": "deleted", "intended": True, "actual": answer, "ok": ok}]}


# ---------- merge ----------

def _ids(raw, what, errors):
    out = []
    for part in values.split(raw or ""):
        if not part.isdigit():
            errors.append(f"{what}: {part!r} is not a contact id")
        elif int(part) not in out:
            out.append(int(part))
    return out


def plan_merge_contacts(ctx, primary_id, with_ids):
    """Merge duplicates into one contact.

    What a live test showed: the primary keeps every value it already has and takes the secondary's
    value only where its own is blank. The secondary's email is kept as a non-primary address, its notes and
    deal links move across, and the secondary is deleted within about a second.
    """
    errors, warnings, changes = [], [], []
    if not str(primary_id).isdigit():
        raise FSError(f"merge-contacts: {primary_id!r} is not a contact id")
    primary_id = int(primary_id)
    secondary_ids = _ids(with_ids, "--with", errors)
    if not secondary_ids and not errors:
        raise FSError("merge-contacts needs --with (comma-separated ids of the duplicate contacts to merge in)")
    if primary_id in secondary_ids:
        errors.append(f"contact {primary_id} can't be merged into itself")
        secondary_ids = [i for i in secondary_ids if i != primary_id]
    primary = reads.get_contact(ctx.client, primary_id)
    secondaries = []
    for sid in secondary_ids:
        try:
            secondaries.append(reads.get_contact(ctx.client, sid))
        except NotFound:
            errors.append(f"contact {sid} does not exist")

    have = {e["value"].lower() for e in primary["emails"] if e.get("value")}
    std = ("first_name", "last_name", "job_title", "mobile_number", "work_number", "linkedin", "city",
           "country", "address", "state", "zipcode")
    taken = {}   # field -> value the primary will take from the first secondary that has one
    for sec in secondaries:
        label = f"{sec['display_name']} (id {sec['id']})"
        for e in sec["emails"]:
            if e.get("value") and e["value"].lower() not in have:
                have.add(e["value"].lower())
                changes.append(_change("email", None, f"{e['value']} (secondary)", None, None) | {"changed": True})
        if sec["notes"]:
            changes.append(_change("notes", None, f"{len(sec['notes'])} from {label} move across", None, None)
                           | {"changed": True})
        for d in sec["deals"]:
            changes.append(_change("deal link", None, f"{d.get('name')} (id {d.get('id')}) moves from {label}",
                                   None, None) | {"changed": True})
        pairs = [(f, primary.get(f), sec.get(f)) for f in std] + [
            (f, primary["custom_field"].get(f), v) for f, v in sorted(sec["custom_field"].items())]
        for field, mine, theirs in pairs:
            if values.empty(theirs) or theirs is False:
                continue
            if values.empty(mine) or mine is False:
                if field not in taken:
                    taken[field] = theirs
                    changes.append(_change(field, None, theirs, "(blank)", f"{theirs}  (filled from {label})")
                                   | {"changed": True})
            elif str(mine).strip().lower() != str(theirs).strip().lower():
                warnings.append(f"{field}: keeps {mine!r}; {label} has {theirs!r}, which is dropped")
        if sec["sales_accounts"] and primary["sales_accounts"] and \
                {a["id"] for a in sec["sales_accounts"]} - {a["id"] for a in primary["sales_accounts"]}:
            warnings.append(f"{label} is linked to account(s) "
                            f"{[a['name'] for a in sec['sales_accounts']]}; check the merged contact's account afterwards")
    head = _change("merge", f"{len(secondaries)} duplicate(s) separate",
                   f"all merged into {primary['display_name']} (id {primary_id})", None, None)
    head.update({"changed": True, "overwrites_existing": True, "flag": "MERGE"})
    changes.insert(0, head)
    return {"op": "merge-contacts", "entity": "contact", "id": primary_id, "label": primary["display_name"],
            "changes": changes, "errors": errors, "warnings": warnings,
            "snapshot": {"primary": primary, "merged_in": secondaries},
            "request": {"method": "POST", "path": "contacts/merge",
                        "body": {"primary_id": primary_id, "secondary_ids": [s["id"] for s in secondaries]}}
            if secondaries else None,
            "_checks": [("merge", {
                "emails": sorted({e["value"] for s in secondaries for e in s["emails"] if e.get("value")}),
                "notes": sorted({n["id"] for s in secondaries for n in s["notes"]} - {n["id"] for n in primary["notes"]}),
                "deals": sorted({d["id"] for s in secondaries for d in s["deals"]} - {d["id"] for d in primary["deals"]})})]}


# Seconds to wait before each re-read while Freshsales finishes a merge. It runs as a background job: in
# live tests the emails and blanks were filled within a couple of seconds, but notes and deal links moved later
# and the duplicates were deleted a few seconds after that.
MERGE_POLLS = (2, 3, 3, 4, 4)


def _merge_gaps(plan, merged):
    """What is still missing from the merged contact: {"emails": [...], "notes": [...], "deals": [...]}."""
    want = plan["_checks"][0][1]
    have_emails = {e["value"].lower() for e in merged["emails"] if e.get("value")}
    have_notes = {n["id"] for n in merged["notes"]}
    have_deals = {d["id"] for d in merged["deals"]}
    return {"emails": [e for e in want["emails"] if e.lower() not in have_emails],
            "notes": [n for n in want["notes"] if n not in have_notes],
            "deals": [d for d in want["deals"] if d not in have_deals]}


def apply_merge(ctx, plan):
    """POST the merge, then wait for it to finish.

    The response is a snapshot taken before the background job is done, so it is checked first (free) and, if
    anything is still missing, the primary is re-read a few times (one call each) until every address, note and
    deal link from the duplicates is on it. If it is still unfinished, the status is PENDING, not an error:
    check again later with `fs contact <id>`.
    """
    req = plan["request"]
    client = ctx.client
    response = client.post(req["path"], req["body"], params={"include": reads.CONTACT_INCLUDE})
    merged = reads.contact_from(response or {})
    polls = 0
    gaps = _merge_gaps(plan, merged) if merged else None
    while (merged is None or any(gaps.values())) and polls < len(MERGE_POLLS):
        client.sleep(MERGE_POLLS[polls])
        polls += 1
        merged = reads.contact_from(client.get(f"contacts/{plan['id']}", params={"include": reads.CONTACT_INCLUDE},
                                               fresh=True))
        gaps = _merge_gaps(plan, merged) if merged else None
    if merged is None:
        return {"status": "MISMATCH", "verification": [{"field": "merge", "intended": "merged contact returned",
                                                       "actual": None, "ok": False}]}
    want = plan["_checks"][0][1]
    rows = [{"field": "primary id", "intended": plan["id"], "actual": merged.get("id"),
             "ok": str(merged.get("id")) == str(plan["id"])},
            {"field": "emails moved", "intended": want["emails"], "actual": [e["value"] for e in merged["emails"]],
             "ok": not gaps["emails"]},
            {"field": "notes moved", "intended": len(want["notes"]), "actual": len(want["notes"]) - len(gaps["notes"]),
             "ok": not gaps["notes"]},
            {"field": "deal links moved", "intended": len(want["deals"]),
             "actual": len(want["deals"]) - len(gaps["deals"]), "ok": not gaps["deals"]}]
    done = all(r["ok"] for r in rows)
    out = {"status": "verified" if done else "PENDING", "verification": rows, "merged_into": plan["id"],
           "removed": req["body"]["secondary_ids"], "polls": polls}
    out["note"] = ("the merged-in contacts are deleted by Freshsales a few seconds after this" if done else
                   "Freshsales has not finished merging yet; check again in a minute with: fs contact " + str(plan["id"]))
    return out
