# CLAUDE.md

Use `fs` for all Freshsales reads and writes. Always run without --apply first, show the diff to the user, and only apply after approval.

- Run `./fs picklists` before writing picklist values; Freshsales silently ignores invalid ones.
- Finding records: `find-contacts` (name/email/city/type, or `--domain` alone), `views` + `list-view` (whole records incl. custom fields; filter with `--where`). All read-only. The account is limited to about 2000 calls an hour, shared with Zapier and everyone else; every run prints what it used and what is left.
- Creates: `create-contact|deal|account`, dry-run first like every write. Use `--preset` for the skill defaults, and never create without checking the duplicate warnings in the dry-run. Multi-selects need `;`-joined values.
- `update-contact` / `update-deal` / `update-account` take any `cf_*` field and `owner`; `add-note --on contact|deal|account`; `link-deal-contacts` adds/removes deal contacts (append and remove, never replace).
- `delete-contact|deal|account` and `merge-contacts` are permanent and guarded: a contact on a live deal or with notes is refused (unlink it first with `link-deal-contacts --remove`), a deal with notes is refused, an account with contacts or deals is refused. There is no "forget". Always show the dry-run and get approval first.
- `update-contact --set email=...` makes it primary and keeps the old address as secondary. `check-contacts` reports the SO-CF priority/status rules and missing city/LinkedIn/email; missing priorities are questions for Karthik, never guesses.
- Call budget: prefer one call that returns more (`--brief`, `list-view`, batches, `--stop-after`) over many small ones; don't loop `lookup` per email; don't use `find-deals --details` unless the stage is needed.
- Never print or commit `FS_API_KEY`; it lives in the environment, the gitignored `.env`, or (in cloud sessions) a network secret used with `FS_AUTH_VIA_PROXY=1`.
- Tests: `python3 -m unittest` (all HTTP is mocked; no live calls).
