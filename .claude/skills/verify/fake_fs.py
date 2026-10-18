"""Local stand-in for the Freshsales API. Logs every request to log.jsonl."""
import json, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = sys.argv[2]
PREFIX = "/crm/sales/api/"
state = {
 "contact": {"id": 11, "display_name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
             "email": "ada@example.com", "job_title": "CEO", "mobile_number": "", "linkedin": None,
             "city": "London", "custom_field": {"cf_rereachout": "No"},
             "sales_accounts": [{"id": 100, "name": "Engines Ltd", "is_primary": True}]},
 "deal": {"id": 5, "name": "Engines deal", "amount": "100.0", "deal_stage_id": 9, "deal_pipeline_id": 1,
          "sales_account_id": 100, "custom_field": {}},
 "notes": [], "hits429": 0, "ignore_city": True, "deal_contacts": [11],
}
OWNERS = [{"id": 1, "value": "Karthik Dasari"}, {"id": 2, "value": "Joanna Te"}]
FIELDS = {"fields": [
 {"name": "cf_rereachout", "label": "Re-reachout", "type": "dropdown",
  "choices": [{"id": 1, "value": "Yes"}, {"id": 2, "value": "No"}, {"id": 4, "value": "Chase"}]},
 {"name": "cf_contact_type_newct", "label": "Contact type", "type": "dropdown",
  "choices": [{"id": 3, "value": "Owner"}, {"id": 7, "value": "TG-Owner/Manager"}, {"id": 8, "value": "SO-CF"}]},
 {"name": "cf_reachout_final_statustargets", "label": "Final status", "type": "dropdown",
  "choices": [{"id": 5, "value": "Meeting Done VC"}, {"id": 6, "value": "No Response"}, {"id": 9, "value": "Unqualified"}]},
 {"name": "first_name", "type": "text", "required": True},
 {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
 {"name": "lifecycle_stage_id", "type": "dropdown", "choices": [{"id": "70", "value": "Lead"}]},
 {"name": "contact_status_id", "type": "dropdown", "choices": [{"id": 71, "value": "Unqualified Leads"}]},
 {"name": "subscription_types", "type": "multi_select_dropdown",
  "choices": [{"id": "1", "value": "Non-marketing emails"}, {"id": "2", "value": "Deal Announcements"}]}]}
def _dd(name, *values):
    return {"name": name, "type": "dropdown", "choices": [{"id": i + 1, "value": v} for i, v in enumerate(values)]}
DEAL_FIELDS = {"fields": [
 {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
 {"name": "deal_pipeline_id", "type": "dropdown", "choices": [{"id": 1, "value": "All Tern Deals"}]},
 {"name": "deal_stage_id", "type": "dropdown", "choices": [{"id": 9, "value": "IM Received"}, {"id": 10, "value": "LOI"},
                                                          {"id": 11, "value": "04 Plan-Teaser/Suspect"}]},
 {"name": "currency_id", "type": "dropdown", "choices": [{"id": 303, "value": "GBP"}]},
 _dd("cf_status", "Healthy", "Distressed"), _dd("cf_deal_currency", "GBP", "EUR"),
 _dd("cf_deal_stagelatest", "10 Suspect"), _dd("cf_chaser_status", "Chase"), _dd("cf_source_deal", "Direct"),
 _dd("cf_deal_type", "Standalone"), _dd("cf_bolton_only", "Yes", "No"), _dd("cf_bolton_platform", "Other")]}
ACCOUNT_FIELDS = {"fields": [
 {"name": "name", "type": "text", "required": True},
 {"name": "owner_id", "type": "dropdown", "choices": OWNERS},
 {"name": "cf_type", "type": "multi_select_dropdown", "choices": [{"id": 1, "value": "SO-CF"}, {"id": 2, "value": "AD-A&T"}]}]}
created = {"contacts": {}, "deals": {}, "sales_accounts": {}}   # records made by POST
next_id = [60]
def readback(coll, key, rec, include):
    """Echo a created record the way Freshsales does: owner, lifecycle, status, currency only via ?include=."""
    out = {k: v for k, v in rec.items() if k not in ("owner_id", "lifecycle_stage_id", "contact_status_id",
                                                     "currency_id", "contacts_added_list", "sales_accounts",
                                                     "custom_field")}
    out["custom_field"] = dict(rec.get("custom_field", {}))
    if "ghost" in str(rec.get("emails")):               # simulate a field that silently does not take
        out.pop("subscription_types", None)
    body = {key: out}
    inc = include.split(",")
    if "owner" in inc and rec.get("owner_id") is not None: out["owner_id"] = rec["owner_id"]
    if key == "contact":
        out["sales_accounts"] = rec.get("sales_accounts", [])
        if "lifecycle_stage" in inc and rec.get("lifecycle_stage_id"): body["lifecycle_stages"] = [{"id": rec["lifecycle_stage_id"]}]
        if "contact_status" in inc and rec.get("contact_status_id"): body["contact_status"] = [{"id": rec["contact_status_id"]}]
    if key == "deal":
        body["contacts"] = [{"id": i} for i in rec.get("contacts_added_list", [])]
        if "currency" in inc and rec.get("currency_id"): body["currencies"] = [{"id": rec["currency_id"]}]
    return body


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def handle_any(self, method):
        u = urllib.parse.urlparse(self.path); path = u.path[len(PREFIX):]; q = dict(urllib.parse.parse_qsl(u.query))
        body = None
        if self.headers.get("Content-Length"):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"null")
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"m": method, "path": path, "q": q, "auth": self.headers.get("Authorization"), "body": body}) + "\n")
        c, d = state["contact"], state["deal"]
        if path == "selector/deal_stages" and state["hits429"] < 2:
            state["hits429"] += 1; return self._send(429, {"message": "slow down"}, {"Retry-After": "1"})
        if method == "GET":
            if path == "settings/contacts/fields": return self._send(200, FIELDS)
            if path == "settings/sales_accounts/fields": return self._send(200, ACCOUNT_FIELDS)
            if path == "settings/deals/fields": return self._send(200, DEAL_FIELDS)
            for coll, key in (("contacts", "contact"), ("deals", "deal"), ("sales_accounts", "sales_account")):
                if path.startswith(coll + "/") and path[len(coll) + 1:].isdigit() and int(path[len(coll) + 1:]) in created[coll]:
                    return self._send(200, readback(coll, key, created[coll][int(path[len(coll) + 1:])], q.get("include", "")))
            if path == "lookup":
                hit = [{"id": 11}] if q.get("q") == c["email"] else []
                hit += [{"id": i, "display_name": r["first_name"]} for i, r in created["contacts"].items()
                        if any(e["value"] == q.get("q") for e in r.get("emails", []))]
                return self._send(200, {"contacts": {"contacts": hit}})
            if path == "contacts/11":
                out = {"contact": c}
                if q.get("include") == "notes": out["notes"] = [n for n in state["notes"] if n.get("on", "Contact") == "Contact"]
                return self._send(200, out)
            if path in ("contacts/filters", "deals/filters", "sales_accounts/filters"):
                return self._send(200, {"filters": [{"id": 1, "name": "All " + path.split("/")[0].title(), "is_default": True, "is_public": True}]})
            if path == "contacts/view/1":
                rows = [dict(c, emails=[{"value": c["email"]}]) | {"owner_id": 1}]
                return self._send(200, {"contacts": rows, "meta": {"total": len(rows), "total_pages": 1}})
            if path == "contacts/12":
                return self._send(200, {"contact": {"id": 12, "display_name": "Bob Babbage", "first_name": "Bob", "last_name": "Babbage",
                                                    "email": "bob@example.com", "custom_field": {}}})
            if path == "search":
                hits = []
                if "engines" in q.get("q", "").lower():
                    hits = [{"id": 5, "type": "deal", "name": d["name"]}, {"id": 100, "type": "sales_account", "name": "Engines Ltd"}]
                if q.get("q") == "New Co":
                    hits = [{"id": 200, "type": "sales_account", "name": "New Co"}]
                if q.get("q") == "Not Qualified":
                    hits = [{"id": 9, "type": "sales_account", "name": "Not Qualified"}]
                return self._send(200, hits)
            if path == "deals/5":
                inc = q.get("include", "").split(",")
                out = {"deal": d, "deal_stages": [{"id": d["deal_stage_id"], "name": {9: "IM Received", 10: "LOI"}[d["deal_stage_id"]]}],
                       "sales_accounts": [{"id": 100, "name": "Engines Ltd"}]}
                if "contacts" in inc: out["contacts"] = [{"id": i, "display_name": f"Contact {i}"} for i in state["deal_contacts"]]
                if "notes" in inc: out["notes"] = [n for n in state["notes"] if n.get("on") == "Deal"]
                return self._send(200, out)
            if path in ("sales_accounts/100",):
                return self._send(200, {"notes": [n for n in state["notes"] if n.get("on") == "SalesAccount"] if "notes" in q.get("include", "") else [],
                                        "sales_account": {"id": 100, "name": "Engines Ltd", "custom_field": {}},
                                        "contacts": [{"id": 11, "display_name": c["display_name"], "email": c["email"]}],
                                        "deals": [{"id": 5, "name": d["name"], "amount": d["amount"], "deal_stage_id": d["deal_stage_id"]}]})
            if path == "selector/deal_stages":
                return self._send(200, {"deal_stages": [{"id": 9, "name": "IM Received", "deal_pipeline_id": 1}, {"id": 10, "name": "LOI", "deal_pipeline_id": 1}]})
        if method == "PUT" and path == "contacts/11":
            upd = dict(body["contact"])
            if state["ignore_city"]: upd.pop("city", None)   # simulate a field silently not taking
            c["custom_field"].update(upd.pop("custom_field", {}))
            c.update(upd); return self._send(200, {"contact": c})
        if method == "PUT" and path == "deals/5":
            upd = dict(body["deal"])
            for i in upd.pop("contacts_added_list", []):            # append, like the live API
                if i not in state["deal_contacts"]: state["deal_contacts"].append(i)
            for i in upd.pop("contacts_removed_list", []):
                if i in state["deal_contacts"]: state["deal_contacts"].remove(i)
            d.update(upd); return self._send(200, {"deal": d})
        if method == "POST" and path == "filtered_search/contact":
            rows = [c]
            for rule in body["filter_rule"]:
                attr, val = rule["attribute"], str(rule["value"]).lower()
                key = {"contact_email.email": "email"}.get(attr, attr)
                rows = [r for r in rows if (rule["operator"] == "contains" and val in str(r.get(key, "")).lower())
                        or (rule["operator"] == "is_in" and str(r.get(key, "")).strip().lower() in val.split(","))]
            return self._send(200, {"contacts": [{k: r.get(k) for k in ("id", "display_name", "email", "job_title", "city")} for r in rows],
                                    "meta": {"total": len(rows)}})
        if method == "POST" and path in ("contacts", "deals", "sales_accounts"):
            key = {"contacts": "contact", "deals": "deal", "sales_accounts": "sales_account"}[path]
            rec = dict(body[key]); rec["id"] = next_id[0]; next_id[0] += 1
            created[path][rec["id"]] = rec
            return self._send(200, {key: {"id": rec["id"]}})
        if method == "POST" and path == "notes":
            n = {"id": 77 + len(state["notes"]), "description": body["note"]["description"], "on": body["note"]["targetable_type"]}
            state["notes"].append(n); return self._send(201, {"note": n})
        return self._send(404, {"errors": {"message": "not found"}})
    def do_GET(self): self.handle_any("GET")
    def do_PUT(self): self.handle_any("PUT")
    def do_POST(self): self.handle_any("POST")
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
