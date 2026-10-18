"""A fake requests.Session that serves canned Freshsales responses."""
import json

from fs_cli.client import BASE_URL, Client


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class Seq:
    """Successive responses for one route; the last one repeats."""

    def __init__(self, *items):
        self.items = list(items)


class FakeSession:
    """routes: {(METHOD, path): body | FakeResponse | callable(params, body) | Seq(...)}"""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url[len(BASE_URL):]
        self.calls.append((method, path, params, json))
        route = self.routes.get((method, path))
        if route is None:
            return FakeResponse(404, {"errors": {"message": "not found"}})
        if isinstance(route, Seq):
            route = route.items.pop(0) if len(route.items) > 1 else route.items[0]
        if callable(route):
            route = route(params, json)
        if isinstance(route, FakeResponse):
            return route
        return FakeResponse(200, route)

    def writes(self):
        # filtered_search is a POST but only reads
        return [c for c in self.calls if c[0] in ("PUT", "POST", "DELETE", "PATCH")
                and not c[1].startswith("filtered_search")]


def make_client(routes):
    session = FakeSession(routes)
    client = Client("test-key", session=session, sleep=lambda s: session.calls.append(("SLEEP", s, None, None)))
    return client, session
