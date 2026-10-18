"""Value parsing, validation and comparison shared by creates and updates.

`fields_entity` is the settings path name: "contacts", "deals" or "sales_accounts".
`noun` only appears in error messages ("contact", "deal", "account").
"""
import re

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "") or v == []


def split(value):
    return [p.strip() for p in re.split(r"[;,]", str(value)) if p.strip()]


def number(value):
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return float(text)


def choice(ctx, fields_entity, field, value, errors, noun):
    """Resolve a choice by its text or numeric id. Returns the id, or None after recording an error."""
    meta = ctx.fields(fields_entity).get(field)
    if meta is None:
        errors.append(f"{field}: no such field on {noun}")
        return None
    wanted = str(value).strip()
    choices = [c for c in (meta.get("choices") or []) if isinstance(c, dict)]
    hits = [c for c in choices if str(c.get("value")).strip() == wanted or str(c.get("id")) == wanted]
    if len(hits) == 1:
        cid = hits[0]["id"]
        return int(cid) if str(cid).isdigit() else cid
    names = [c.get("value") for c in choices]
    shown = names[:15] + (["..."] if len(names) > 15 else [])
    errors.append(f"{field}: {value!r} is {'ambiguous' if hits else 'not a valid choice'}. Valid: {shown}")
    return None


def choice_name(ctx, fields_entity, field, cid):
    """The display text for a choice id, or the id itself when unknown."""
    meta = ctx.fields(fields_entity).get(field) or {}
    for c in meta.get("choices") or []:
        if isinstance(c, dict) and str(c.get("id")) == str(cid):
            return c.get("value")
    return cid


def custom(ctx, fields_entity, field, value, errors, noun):
    """Validate a cf_* value against its field type.

    Returns (payload_value, kind, expected) or None after recording an error.
    """
    meta = ctx.fields(fields_entity).get(field)
    if meta is None:
        errors.append(f"{field}: no such custom field on {noun}")
        return None
    ftype = meta.get("type")
    valid = [c.get("value") for c in (meta.get("choices") or []) if isinstance(c, dict)]
    text = str(value).strip()
    if ftype in ("dropdown", "radio"):
        if text not in valid:
            errors.append(f"{field}: {value!r} is not a valid picklist value. Valid: {valid}")
            return None
        return text, "text", text
    if ftype == "multi_select_dropdown":
        parts = split(value)
        bad = [p for p in parts if p not in valid]
        if bad or not parts:
            errors.append(f"{field}: {bad or value!r} not valid. Valid: {valid[:15]}"
                          f"{'...' if len(valid) > 15 else ''}")
            return None
        return ";".join(parts), "set", set(parts)
    if ftype == "checkbox":
        if text.lower() not in ("true", "false", "yes", "no"):
            errors.append(f"{field}: checkbox needs true or false, got {value!r}")
            return None
        flag = text.lower() in ("true", "yes")
        return flag, "bool", flag
    if ftype == "number":
        try:
            return number(text), "num", number(text)
        except ValueError:
            errors.append(f"{field}: {value!r} is not a number")
            return None
    if ftype == "date":
        if not DATE_RE.match(text):
            errors.append(f"{field}: {value!r} is not a date (YYYY-MM-DD)")
            return None
        return text, "date", text
    if ftype in ("text", "textarea"):
        return text, "text", text
    errors.append(f"{field}: fields of type {ftype!r} are not supported")
    return None


def _digits(value):
    return re.sub(r"[^\d+]", "", str(value or ""))


def compare(kind, expected, actual):
    if kind == "date":  # Freshsales returns 2026-11-01T00:00:00+00:00 for a saved 2026-11-01
        return (empty(expected) and empty(actual)) or (
            not empty(actual) and str(actual)[:10] == str(expected)[:10])
    if kind == "phone":  # numbers come back with the spaces stripped
        return (empty(expected) and empty(actual)) or (
            not empty(actual) and _digits(actual) == _digits(expected))
    if kind == "text":
        return (empty(expected) and empty(actual)) or (
            not empty(actual) and str(actual).strip() == str(expected).strip())
    if kind == "num":
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    if kind == "id":
        return actual is not None and str(actual) == str(expected)
    if kind == "bool":
        return bool(actual) == bool(expected)
    if kind == "set":
        parts = split(actual) if isinstance(actual, str) else [str(a) for a in actual or []]
        return set(parts) == {str(e) for e in expected}
    if kind == "has":
        return str(expected) in {str(a) for a in actual or []}
    if kind == "all":
        return {str(e) for e in expected} <= {str(a) for a in actual or []}
    if kind == "exact":
        return {str(e) for e in expected} == {str(a) for a in actual or []}
    if kind == "emails":
        return [e.lower() for e in expected] == [str(a).lower() for a in (actual or [])][:len(expected)]
    return False
