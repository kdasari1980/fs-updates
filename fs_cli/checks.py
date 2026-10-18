"""Tern's standing rules for contacts, as checks that report problems rather than block anything.

From the fs-update skill:
- SO-CF contacts have a priority of P1 or P3 only; P3 means Reachout Status "NA/Not Relevant".
  A missing priority is a question for Karthik, never a guess.
- Every contact being updated needs a city, a LinkedIn profile and an email.

The checks work on any contact-shaped dict with `custom_field`, `city`, `linkedin` and `email` (or `emails`):
a summary from `get_contact`, a row from a saved view, or the state a pending update would leave behind.
"""
SO_CF = "SO-CF"
NA_STATUS = "NA/Not Relevant"
ALLOWED_PRIORITIES = ("P1", "P3")


def _cf(record, name):
    return (record.get("custom_field") or {}).get(name)


def _has(value):
    return value is not None and str(value).strip() != ""


def _has_email(record):
    if _has(record.get("email")):
        return True
    return any(_has(e.get("value")) for e in record.get("emails") or [] if isinstance(e, dict))


def contact_issues(record):
    """[(code, message), ...] for one contact. Empty when it meets the rules."""
    issues = []
    if _cf(record, "cf_contact_type_newct") == SO_CF:
        priority, status = _cf(record, "cf_reachout_priority"), _cf(record, "cf_rereachout")
        if not _has(priority):
            issues.append(("so_cf_no_priority", "SO-CF contact has no priority: ask Karthik for P1 or P3"))
        elif priority not in ALLOWED_PRIORITIES:
            issues.append(("so_cf_bad_priority", f"SO-CF priority is {priority}: must be P1 or P3, ask Karthik which"))
        elif priority == "P3" and status != NA_STATUS:
            issues.append(("so_cf_p3_status", f"SO-CF P3 must have Reachout Status {NA_STATUS} (is {status or 'empty'})"))
    for code, label, present in (("no_city", "city", _has(record.get("city"))),
                                 ("no_linkedin", "LinkedIn", _has(record.get("linkedin"))),
                                 ("no_email", "email", _has_email(record))):
        if not present:
            issues.append((code, f"{label} is missing"))
    return issues


def summarise(records):
    """Counts for the report line the skill asks for, plus the contacts behind each problem."""
    by_code, per_contact = {}, []
    so_cf = 0
    for rec in records:
        if _cf(rec, "cf_contact_type_newct") == SO_CF:
            so_cf += 1
        issues = contact_issues(rec)
        if issues:
            per_contact.append({"id": rec.get("id"), "name": rec.get("display_name"),
                                "issues": [m for _, m in issues], "codes": [c for c, _ in issues]})
        for code, _ in issues:
            by_code.setdefault(code, []).append(rec.get("id"))
    return {"checked": len(records), "so_cf_checked": so_cf,
            "so_cf_no_priority": len(by_code.get("so_cf_no_priority", [])),
            "so_cf_bad_priority": len(by_code.get("so_cf_bad_priority", [])),
            "so_cf_p3_status_to_fix": len(by_code.get("so_cf_p3_status", [])),
            "missing_city": len(by_code.get("no_city", [])),
            "missing_linkedin": len(by_code.get("no_linkedin", [])),
            "missing_email": len(by_code.get("no_email", [])),
            "contacts_with_issues": per_contact}


def p3_status_fixes(records):
    """The mechanical fix the skill allows: P3 SO-CF contacts get Reachout Status NA/Not Relevant.

    Returns batch items for `update-contact --batch`, each noting the status it would replace,
    because replacing it overwrites outreach history such as 1st Contact or Meeting Done.
    """
    fixes = []
    for rec in records:
        if _cf(rec, "cf_contact_type_newct") == SO_CF and _cf(rec, "cf_reachout_priority") == "P3" \
                and _cf(rec, "cf_rereachout") != NA_STATUS:
            fixes.append({"id": rec.get("id"), "set": {"cf_rereachout": NA_STATUS},
                          "replaces": _cf(rec, "cf_rereachout") or "(empty)", "name": rec.get("display_name")})
    return fixes
