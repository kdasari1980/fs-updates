# fs: Freshsales CLI

A small Python 3 CLI that talks directly to the Freshsales (Freshworks CRM) REST API for
`tern.freshworks.com`, replacing the rate-limited Zapier actions.

- Base URL: `https://tern.freshworks.com/crm/sales/api/`
- Dependencies: Python 3.9+ and `requests` (`pip install -r requirements.txt`)
- JSON goes to **stdout**. Diffs, verification tables and errors go to **stderr**. Exit code is non-zero on failure.

## Setup

```bash
pip install -r requirements.txt
echo 'FS_API_KEY=your-key-here' > .env   # .env is gitignored
# or: export FS_API_KEY=...
./fs picklists                            # quick connectivity check
```

The key is read from `FS_API_KEY` in the environment first, then from `.env` in the current
directory or the repo directory. It is only sent in the `Authorization: Token token=...` header
and is never printed.

## Read commands

```bash
./fs lookup ada@example.com,bob@example.com
```
Returns one entry per email: `status: found` with the full contact (id, display_name, names,
job_title, mobile_number, linkedin, city, all `custom_field` values, and linked sales accounts as
id + name), or `status: not found`. A miss does not fail the batch. Only HTTP errors give exit 1.

```bash
./fs find-deals "Acme,Acme Software Ltd"
```
Full-text search per term (`/search?include=deal,sales_account`). Returns the matching deals (id, name,
stage, amount, custom fields, linked account) and the matching sales accounts.

```bash
./fs account 70001234567
```
The account record plus its linked contacts and deals.

```bash
./fs picklists
```
Valid values for `cf_rereachout`, `cf_contact_type_newct` and `cf_reachout_final_statustargets`.

## Write commands (dry-run by default)

Without `--apply`, a write command reads the current record and prints a before/after diff.
Nothing is sent. A row flagged `OVERWRITE` would replace a non-empty existing value.

```bash
./fs update-contact 70001234567 \
    --set job_title="Managing Director" \
    --set cf_reachout_final_statustargets="Meeting Done VC" \
    --set sales_account="Acme Software Ltd"
```

Example diff (stderr):
```
== update-contact contact 70001234567 (Ada Lovelace) ==
field                            before       after
-------------------------------  -----------  ----------------------------------------  ---------
job_title                        CEO          Managing Director                         OVERWRITE
cf_reachout_final_statustargets  (empty)      Meeting Done VC                           change
sales_account                    Engines Ltd  Acme Software Ltd (primary), Engines Ltd  OVERWRITE

DRY RUN: no changes sent. Re-run with --apply to write.
```

When you're happy with the diff, re-run the same command with `--apply`. That will:
1. Validate picklist values against the live field settings and reject invalid ones **before**
   sending anything. For example, `Meeting Done` is rejected and `Meeting Done VC` is accepted.
2. Send the PUT or POST.
3. Re-read the record and print a `field / intended / actual` table. It exits 1 if any field did not take.

Supported contact keys: `first_name`, `last_name`, `job_title`, `mobile_number`, `linkedin`, `city`,
`sales_account` (exact existing account name; errors if there is no match or more than one match), plus the three `cf_` picklists above.
Setting `sales_account` makes that account primary and keeps the contact's existing accounts as
non-primary. The full list is sent because the API docs don't say whether a PUT replaces
the existing links. `key=` (an empty value) clears a field.

```bash
./fs add-note 70001234567 "Met at conference; follow up in Q1"          # dry-run
./fs add-note 70001234567 "Met at conference; follow up in Q1" --apply
```

```bash
./fs update-deal 70009876543 --set deal_stage="LOI" --set amount=2500000 --set cf_some_field=value
```
Supported deal keys: `name`, `amount`, `expected_close`, `probability`, `deal_stage` (by stage
name, resolved within the deal's pipeline), `sales_account` (exact name), and any `cf_*` field
that exists on deals. Dropdown values are validated.

### Batch

Each write command accepts `--batch FILE`. FILE holds a JSON list of operations (or `{"operations": [...]}`).
It is still dry-run unless you pass `--apply`. With `--apply`, every operation is validated first. If any one is
invalid, **nothing** is sent.

```json
[
  {"id": 70001234567, "set": {"job_title": "CFO", "cf_rereachout": "Yes"}},
  {"id": 70001234568, "set": {"sales_account": "Acme Software Ltd"}}
]
```
```bash
./fs update-contact --batch contacts.json            # review diffs
./fs update-contact --batch contacts.json --apply
./fs add-note --batch notes.json                      # [{"id": 1, "text": "..."}]
./fs update-deal --batch deals.json                   # [{"id": 1, "set": {...}}]
```
An item may carry `"op"`. If it does, it must match the command.

## Rate limits

Freshsales allows 1000 requests per hour per account. On HTTP 429 the client retries up to 3 times.
It honours `Retry-After` when that is 120s or less, and otherwise backs off 2s, 4s, 8s. If `Retry-After` is longer than 120s, it fails immediately.
`lookup` and `find-deals` make one request per result, so batch sizes eat into the hourly budget.

## Not supported

Delete and merge are deliberately absent, because both are permanent.

## Endpoints used

Checked against https://developers.freshworks.com/crm/api/:

| Purpose | Call |
|---|---|
| Email lookup | `GET lookup?q=<email>&f=email&entities=contact` |
| Contact | `GET contacts/{id}?include=sales_accounts`, `PUT contacts/{id}` `{"contact": {..., "custom_field": {...}, "sales_accounts": [{"id", "is_primary"}]}}` |
| Search | `GET search?q=<term>&include=deal,sales_account&per_page=100` |
| Deal | `GET deals/{id}?include=sales_account,deal_stage`, `PUT deals/{id}` `{"deal": {...}}` |
| Account | `GET sales_accounts/{id}?include=contacts,deals` |
| Fields/picklists | `GET settings/{contacts,sales_accounts,deals}/fields` (`choices[].value`) |
| Deal stages | `GET selector/deal_stages` |
| Note | `POST notes` `{"note": {"description", "targetable_type": "Contact", "targetable_id"}}` |

The docs don't say where included associations appear in the response (inside the record or at the
top level). The parser accepts both.

## Tests

```bash
python3 -m unittest -v
```
All HTTP is mocked (`tests/fakes.py`). The tests make no network calls.
