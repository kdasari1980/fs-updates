"""Small on-disk cache for Freshsales metadata that almost never changes.

Field definitions, picklist choices, deal stages and the list of saved views cost one API call each
and are re-read by every command. With the account allowed about 1000 calls an hour, a
dry-run then apply of a single update would spend 2-3 of them on metadata alone. Entries live for
`FS_CACHE_TTL` seconds (default 3600). Only metadata is cached, never records and never the API key.

    FS_NO_CACHE=1        turn it off
    FS_CACHE_TTL=600     change the lifetime in seconds
    FS_CACHE_DIR=...     where the file lives (default ~/.cache/fs-cli)
    fs --refresh ...     ignore what is stored and fetch fresh copies (they are stored again)

Every failure to read or write the file is ignored: the cache is an optimisation, never a dependency.
"""
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULT_TTL = 3600


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def default_dir(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("FS_CACHE_DIR"):
        return Path(environ["FS_CACHE_DIR"])
    base = environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "fs-cli"


class MetaCache:
    def __init__(self, directory=None, ttl=DEFAULT_TTL, refresh=False, now=time.time):
        self.directory = Path(directory) if directory else default_dir()
        self.ttl = ttl
        self.refresh = refresh
        self.now = now
        self.oldest_hit = None  # age in seconds of the oldest entry served from disk this run
        self._data = None

    @classmethod
    def from_env(cls, environ=None, refresh=False):
        """The cache the CLI should use, or None when FS_NO_CACHE is set."""
        environ = os.environ if environ is None else environ
        if _truthy(environ.get("FS_NO_CACHE")):
            return None
        try:
            ttl = int(environ.get("FS_CACHE_TTL", DEFAULT_TTL))
        except ValueError:
            ttl = DEFAULT_TTL
        return cls(default_dir(environ), ttl=ttl, refresh=refresh)

    @property
    def path(self):
        return self.directory / "meta.json"

    @staticmethod
    def _key(base_url, path, params):
        return f"{base_url}|{path}|{json.dumps(sorted((params or {}).items()), default=str)}"

    def _load(self):
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
                if not isinstance(self._data, dict):
                    self._data = {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, base_url, path, params=None):
        """The stored response, or None when missing, expired or a refresh was requested."""
        if self.refresh or self.ttl <= 0:
            return None
        entry = self._load().get(self._key(base_url, path, params))
        if not isinstance(entry, dict) or "data" not in entry:
            return None
        age = self.now() - entry.get("t", 0)
        if age < 0 or age > self.ttl:
            return None
        self.oldest_hit = age if self.oldest_hit is None else max(self.oldest_hit, age)
        return entry["data"]

    def put(self, base_url, path, params, data):
        try:
            store = self._load()
            store[self._key(base_url, path, params)] = {"t": self.now(), "data": data}
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".meta-", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(store, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            pass

    def note(self):
        """A hint to append to validation errors when stored metadata may be out of date."""
        if self.oldest_hit is None or self.oldest_hit < 60:
            return ""
        return (f" (picklists and fields come from a cache {int(self.oldest_hit // 60)} min old; "
                "re-run with --refresh if they were changed in Freshsales)")
