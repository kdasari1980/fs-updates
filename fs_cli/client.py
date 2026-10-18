"""HTTP client for the Freshsales REST API.

The API key is only ever placed in the Authorization header; it is never
logged, echoed or included in error messages.

With FS_AUTH_VIA_PROXY=1 the client sends no Authorization header at all and
relies on an outbound proxy (e.g. a Claude Code cloud "network secret") to
attach it.
"""
import copy
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "https://tern.freshworks.com/crm/sales/api/"
MAX_RETRIES = 3
BACKOFF_SECONDS = (2, 4, 8)
# Freshsales limits are per hour; refuse to sleep longer than this on a 429.
MAX_RETRY_AFTER = 120
TIMEOUT = 30


class FSError(Exception):
    """Any failure talking to Freshsales or validating input."""


class NotFound(FSError):
    """HTTP 404 from Freshsales."""


def _env_file_candidates():
    here = Path(__file__).resolve().parent.parent
    return [Path.cwd() / ".env", here / ".env"]


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_api_key(environ=None, env_files=None):
    """Return the API key, or None when auth is delegated to a proxy."""
    environ = os.environ if environ is None else environ
    if _truthy(environ.get("FS_AUTH_VIA_PROXY")):
        return None
    key = (environ.get("FS_API_KEY") or "").strip()
    if key:
        return key
    for path in env_files if env_files is not None else _env_file_candidates():
        path = Path(path)
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            if name == "FS_API_KEY":
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise FSError("FS_API_KEY is not set (set the env var or add it to a .env file, "
                  "or set FS_AUTH_VIA_PROXY=1 if a proxy injects the Authorization header)")


def _body_snippet(resp, limit=300):
    text = (resp.text or "").strip().replace("\n", " ")
    return text[:limit]


RATE_HEADERS = ("X-Ratelimit-Limit", "X-Ratelimit-Remaining", "X-Ratelimit-Reset",
                "Per-Min-X-Ratelimit-Limit", "Per-Min-X-Ratelimit-Remaining")


class Client:
    """One API client per run. Every HTTP request is counted, because the account is limited to
    roughly 1000 calls an hour and Freshsales counts retries too. GET responses are remembered for the
    rest of the run (a write clears them) so the same record is never fetched twice."""

    def __init__(self, api_key, base_url=BASE_URL, session=None, sleep=time.sleep, disk_cache=None):
        self.base_url = base_url
        self.sleep = sleep
        self.disk_cache = disk_cache
        self.calls = 0       # HTTP requests sent, retries included
        self.retries = 0     # of which were 429 retries
        self.write_calls = 0
        self.rate = {}       # latest rate-limit headers Freshsales sent back
        self._memo = {}
        self.via_proxy = api_key is None
        self.session = session or requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        if not self.via_proxy:
            self.session.headers["Authorization"] = f"Token token={api_key}"

    def _retry_wait(self, resp, attempt):
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = BACKOFF_SECONDS[attempt]
            if wait > MAX_RETRY_AFTER:
                return None
            return wait
        return BACKOFF_SECONDS[attempt]

    def _note_rate(self, resp):
        for name in RATE_HEADERS:
            value = (resp.headers or {}).get(name)
            if value is not None:
                self.rate[name] = value

    @property
    def remaining(self):
        """Calls left in the account's current window as last reported by Freshsales, or None."""
        try:
            return int(self.rate["X-Ratelimit-Remaining"])
        except (KeyError, ValueError):
            return None

    def meta_note(self):
        return self.disk_cache.note() if self.disk_cache else ""

    def usage(self):
        """One line for the end of a run: what this run spent and what the account has left."""
        if not self.calls:
            return None
        text = f"API calls this run: {self.calls}"
        extra = []
        if self.write_calls:
            extra.append(f"{self.write_calls} writes")
        if self.retries:
            extra.append(f"{self.retries} retries")
        if extra:
            text += f" ({', '.join(extra)})"
        left, limit = self.remaining, self.rate.get("X-Ratelimit-Limit")
        if left is not None:
            text += f"; account has {left}" + (f" of {limit}" if limit else "") + " left in this window"
            try:  # the reset header is DDMMYYHHMMSS in UTC
                text += f", refilling at {datetime.strptime(self.rate['X-Ratelimit-Reset'], '%d%m%y%H%M%S'):%H:%M} UTC"
            except (KeyError, ValueError):
                pass
        return text

    def request(self, method, path, params=None, body=None, read=False):
        """`read=True` marks a POST that only reads (filtered_search), so it doesn't clear remembered GETs."""
        url = self.base_url + path.lstrip("/")
        attempt = 0
        is_write = method != "GET" and not read
        while True:
            self.calls += 1
            if is_write:
                self.write_calls += 1
            try:
                resp = self.session.request(method, url, params=params, json=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise FSError(f"{method} {path}: network error ({exc.__class__.__name__})") from None
            self._note_rate(resp)
            if resp.status_code != 429:
                break
            if attempt >= MAX_RETRIES:
                raise FSError(f"{method} {path}: HTTP 429 rate limited; gave up after {MAX_RETRIES} retries")
            wait = self._retry_wait(resp, attempt)
            if wait is None:
                raise FSError(f"{method} {path}: HTTP 429 rate limited; Retry-After "
                              f"{resp.headers.get('Retry-After')}s is too long, try again later")
            attempt += 1
            self.retries += 1
            print(f"HTTP 429 rate limited; retry {attempt}/{MAX_RETRIES} in {wait:g}s", file=sys.stderr)
            self.sleep(wait)

        if resp.status_code == 401 and self.via_proxy:
            raise FSError(f"{method} {path}: HTTP 401 unauthorised. FS_AUTH_VIA_PROXY is set, so check the "
                          "network secret for tern.freshworks.com (header Authorization, "
                          "value 'Token token=<key>', no prefix)")
        if resp.status_code == 404:
            raise NotFound(f"{method} {path}: not found (HTTP 404)")
        if resp.status_code >= 400:
            raise FSError(f"{method} {path}: HTTP {resp.status_code}: {_body_snippet(resp)}")
        if is_write:
            self._memo.clear()  # anything remembered may now be out of date
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise FSError(f"{method} {path}: response was not JSON") from None

    def get(self, path, params=None, meta=False, fresh=False):
        """GET, remembered for the rest of the run. `meta=True` also keeps it on disk (see cache.py).
        `fresh=True` always asks Freshsales again (for polling something that is changing)."""
        key = (path, tuple(sorted((params or {}).items())))
        if fresh:
            data = self.request("GET", path, params=params)
            self._memo[key] = data
            return copy.deepcopy(data)
        if key in self._memo:
            return copy.deepcopy(self._memo[key])
        data = None
        if meta and self.disk_cache is not None:
            data = self.disk_cache.get(self.base_url, path, params)
        if data is None:
            data = self.request("GET", path, params=params)
            if meta and self.disk_cache is not None:
                self.disk_cache.put(self.base_url, path, params, data)
        self._memo[key] = data
        return copy.deepcopy(data)

    def put(self, path, body, params=None):
        return self.request("PUT", path, params=params, body=body)

    def post(self, path, body, params=None):
        return self.request("POST", path, params=params, body=body)

    def delete(self, path):
        return self.request("DELETE", path)
