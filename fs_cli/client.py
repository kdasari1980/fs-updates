"""HTTP client for the Freshsales REST API.

The API key is only ever placed in the Authorization header; it is never
logged, echoed or included in error messages.
"""
import os
import sys
import time
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


def load_api_key(environ=None, env_files=None):
    environ = os.environ if environ is None else environ
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
    raise FSError("FS_API_KEY is not set (set the env var or add it to a .env file)")


def _body_snippet(resp, limit=300):
    text = (resp.text or "").strip().replace("\n", " ")
    return text[:limit]


class Client:
    def __init__(self, api_key, base_url=BASE_URL, session=None, sleep=time.sleep):
        self.base_url = base_url
        self.sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Token token={api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

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

    def request(self, method, path, params=None, body=None):
        url = self.base_url + path.lstrip("/")
        attempt = 0
        while True:
            try:
                resp = self.session.request(method, url, params=params, json=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise FSError(f"{method} {path}: network error ({exc.__class__.__name__})") from None
            if resp.status_code != 429:
                break
            if attempt >= MAX_RETRIES:
                raise FSError(f"{method} {path}: HTTP 429 rate limited; gave up after {MAX_RETRIES} retries")
            wait = self._retry_wait(resp, attempt)
            if wait is None:
                raise FSError(f"{method} {path}: HTTP 429 rate limited; Retry-After "
                              f"{resp.headers.get('Retry-After')}s is too long, try again later")
            attempt += 1
            print(f"HTTP 429 rate limited; retry {attempt}/{MAX_RETRIES} in {wait:g}s", file=sys.stderr)
            self.sleep(wait)

        if resp.status_code == 404:
            raise NotFound(f"{method} {path}: not found (HTTP 404)")
        if resp.status_code >= 400:
            raise FSError(f"{method} {path}: HTTP {resp.status_code}: {_body_snippet(resp)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise FSError(f"{method} {path}: response was not JSON") from None

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def put(self, path, body):
        return self.request("PUT", path, body=body)

    def post(self, path, body):
        return self.request("POST", path, body=body)
