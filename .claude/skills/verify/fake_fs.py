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
 "notes": [], "hits429": 0, "ignore_city": True,
}
FIELDS = {"fields": [
 {"name": "cf_rereachout", "label": "Re-reachout", "type": "dropdown", "choices": [{"id": 1, "value": "Yes"}, {"id": 2, "value": "No"}]},
 {"name": "cf_contact_type_newct", "label": "Contact type", "type": "dropdown", "choices": [{"id": 3, "value": "Owner"}]},
 {"name": "cf_reachout_final_statustargets", "label": "Final status", "type": "dropdown",
  "choices": [{"id": 5, "value": "Meeting Done VC"}, {"id": 6, "value": "No Response"}]}]}
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
            if path in ("settings/sales_accounts/fields", "settings/deals/fields"): return self._send(200, {"fields": []})
            if path == "lookup":
                hit = [{"id": 11}] if q.get("q") == c["email"] else []
                return self._send(200, {"contacts": {"contacts": hit}})
            if path == "contacts/11":
                out = {"contact": c}
                if q.get("include") == "notes": out["notes"] = state["notes"]
                return self._send(200, out)
            if path == "search":
                hits = []
                if "engines" in q.get("q", "").lower():
                    hits = [{"id": 5, "type": "deal", "name": d["name"]}, {"id": 100, "type": "sales_account", "name": "Engines Ltd"}]
                if q.get("q") == "New Co":
                    hits = [{"id": 200, "type": "sales_account", "name": "New Co"}]
                return self._send(200, hits)
            if path == "deals/5":
                return self._send(200, {"deal": d, "deal_stages": [{"id": d["deal_stage_id"], "name": {9: "IM Received", 10: "LOI"}[d["deal_stage_id"]]}],
                                        "sales_accounts": [{"id": 100, "name": "Engines Ltd"}]})
            if path in ("sales_accounts/100",):
                return self._send(200, {"sales_account": {"id": 100, "name": "Engines Ltd", "custom_field": {}},
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
            d.update(body["deal"]); return self._send(200, {"deal": d})
        if method == "POST" and path == "notes":
            n = {"id": 77 + len(state["notes"]), "description": body["note"]["description"]}
            state["notes"].append(n); return self._send(201, {"note": n})
        return self._send(404, {"errors": {"message": "not found"}})
    def do_GET(self): self.handle_any("GET")
    def do_PUT(self): self.handle_any("PUT")
    def do_POST(self): self.handle_any("POST")
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
