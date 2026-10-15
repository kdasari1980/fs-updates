# CLAUDE.md

Use `fs` for all Freshsales reads and writes. Always run without --apply first, show the diff to the user, and only apply after approval.

- Run `./fs picklists` before writing picklist values; Freshsales silently ignores invalid ones.
- Delete and merge are deliberately not supported (both are permanent).
- Never print or commit `FS_API_KEY`; it lives in the environment or the gitignored `.env`.
- Tests: `python3 -m unittest` (all HTTP is mocked; no live calls).
