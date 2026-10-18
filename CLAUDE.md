# CLAUDE.md

Use `fs` for all Freshsales reads and writes. Always run without --apply first, show the diff to the user, and only apply after approval.

- Run `./fs picklists` before writing picklist values; Freshsales silently ignores invalid ones.
- Creates: `create-contact|deal|account`, dry-run first like every write. Use `--preset` for the skill defaults, and never create without checking the duplicate warnings in the dry-run. Multi-selects need `;`-joined values.
- Delete and merge are deliberately not supported (both are permanent).
- Never print or commit `FS_API_KEY`; it lives in the environment, the gitignored `.env`, or (in cloud sessions) a network secret used with `FS_AUTH_VIA_PROXY=1`.
- Tests: `python3 -m unittest` (all HTTP is mocked; no live calls).
