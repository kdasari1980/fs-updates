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

### Claude Code cloud sessions (network secret)

In a Claude Code cloud environment, store the key as a **network secret** rather than an
environment variable, because environment variables are readable by anyone using the environment:

- Allowed websites: `tern.freshworks.com`
- Custom header: name `Authorization`, prefix **cleared**, value `Token token=<your key>`
- Environment variable (not secret): `FS_AUTH_VIA_PROXY=1`

With `FS_AUTH_VIA_PROXY=1`, `fs` sends no `Authorization` header and the proxy attaches it.
A 401 in this mode points at the network secret.

### Local use

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

Supported contact keys: `first_name`, `last_name`, `job_title`, `mobile_number`, `work_number`, `linkedin`, `city`,
`country`, `address`, `state`, `zipcode`, `owner` (name or id), `sales_account` (exact existing account name; errors if
there is no match or more than one match), and **any `cf_*` custom field**. Custom fields are checked by type:
dropdowns against their choices, multi-selects value by value (`A;B`, order doesn't matter), checkboxes `true`/`false`,
numbers, `YYYY-MM-DD` dates and free text. `auto_complete` fields (such as `cf_reports_to`) are rejected.
Email changes are not supported yet.
Setting `sales_account` makes that account primary and keeps the contact's existing accounts as
non-primary. The full list is sent because the API docs don't say whether a PUT replaces
the existing links. `key=` (an empty value) clears a field.

```bash
./fs add-note 70001234567 "Met at conference; follow up in Q1"          # dry-run
./fs add-note 70001234567 "Met at conference; follow up in Q1" --apply
./fs add-note 70009876543 "Teaser received" --on deal --apply            # --on contact (default) | deal | account
```

```bash
./fs link-deal-contacts 70009876543 --add 70001234567,70001234568 --remove 70001234569    # dry-run
./fs link-deal-contacts 70009876543 --add 70001234567 --apply
```
Adds and/or removes contacts on a deal. Only the changes are sent: on the live API `contacts_added_list`
**appends** (ids already linked are harmless) and `contacts_removed_list` removes. Contacts must exist,
a contact in both lists is an error, and removing one that isn't linked is a warning. After applying, the deal's
contacts are read back and must equal the intended final set exactly. A warning is printed if the deal
would be left with no contacts. (The Zapier action replaced the whole list. The API does not.)

```bash
./fs update-deal 70009876543 --set deal_stage="LOI" --set amount=2500000 --set cf_some_field=value
```
Supported deal keys: `name`, `amount`, `expected_close`, `probability`, `deal_stage` (by stage
name, resolved within the deal's pipeline), `owner` (name or id), `sales_account` (exact name), and any `cf_*` field
that exists on deals, checked by type like contact custom fields. Use `link-deal-contacts` to change a deal's contacts.

### Create commands

```bash
./fs create-contact --preset target-contact --set first_name=Ada --set last_name=Lovelace \
    --set email=ada@example.com --set job_title=CEO --set city=Reading        # dry-run
./fs create-deal    --preset target-deal --set name="Acme Software" --set contacts=70001234567
./fs create-account --preset advisor-account --set name="Acme Advisers LLP" --set "cf_type=SO-CF;AD-A&T"
```
Dry-run by default, like every write. A create plan is validated against the live field settings and
checked for duplicates before anything is sent. `--apply` then POSTs, re-reads the new record and prints
`field / intended / actual`, the new id and a Freshsales link. It exits 1 on any mismatch.

**Presets** carry the `fs-update` skill's defaults. They appear in the diff marked `default`, any `--set`
overrides them, and they are validated against the live picklists like every other value:

| Preset | Sets |
|---|---|
| `target-contact` | owner Karthik Dasari, lifecycle Lead, status Unqualified Leads, type `TG-Owner/Manager`, final status `Unqualified`, reachout `Chase`, subscription Deal Announcements, account `Not Qualified` |
| `advisor-contact` | owner Karthik Dasari, reachout `Chase`, final status `Unqualified` (you give type, priority and account) |
| `target-deal` | amount -1, owner Karthik Dasari, pipeline All Tern Deals, stage `04 Plan-Teaser/Suspect`, `10 Suspect`, Chase, Direct, Healthy, Standalone, Bolt-on Yes / Other, GBP |
| `advisor-account` | owner Karthik Dasari |

Keys:
* **Contact:** `first_name` (required), `last_name`, `email` / `emails` (`;` or `,` separated, the first is primary; an email or `mobile_number` is required), `job_title`, `mobile_number`, `work_number`, `linkedin`, `city`, `country`, `address`, `state`, `zipcode`, `owner`, `lifecycle_stage`, `contact_status`, `lead_source`, `territory`, `subscription_types` (names or ids, `;` separated), `sales_account` (exact name, made primary), any `cf_*`.
* **Deal:** `name` and `amount` (required), `expected_close`, `probability`, `owner`, `stage` / `deal_stage`, `pipeline`, `currency`, `lead_source`, `territory`, `sales_account` (exact name), `contacts` (existing contact ids, comma separated), any `cf_*`.
* **Account:** `name` (required), `website`, `phone`, `address`, `city`, `state`, `zipcode`, `country`, `annual_revenue`, `owner`, `industry_type`, `business_type`, `territory`, any `cf_*`.
* Choice fields (`owner`, `stage`, ...) take the name or the numeric id. Custom fields are checked by type: dropdowns against their choices, multi-selects value by value, checkboxes `true`/`false`, numbers and `YYYY-MM-DD` dates. Types that can't be set from text (`auto_complete`, such as `cf_reports_to`) are rejected.

**Duplicates.** A contact whose email already exists, or an account or deal whose name exactly matches an
existing one (ignoring case), is an error and blocks the whole batch. Look-alikes (the name without
trailing Ltd/Group/etc.) are warnings. The search index lags new records by a few minutes, so a record
created moments ago is not seen by the duplicate check. Duplicates inside one batch are caught.

Facts confirmed against the live API with throwaway records (all deleted afterwards):
* dropdown, radio and checkbox custom fields accept their text values;
* a multi-select (`cf_type`) needs one `;`-joined string. A list or comma string silently drops values;
* a contact can be linked to an account at creation (`sales_accounts`), and a deal to contacts (`contacts_added_list`);
* `subscription_types` takes a `;`-joined string of ids;
* owner, lifecycle stage, contact status and currency only show on a read-back with
  `?include=owner,lifecycle_stage,contact_status,currency`, so the verification asks for them.

A batch is `[{"preset": "advisor-contact", "set": {...}}, ...]` (no id). Creates in one batch cannot refer to
each other: create the contacts first, then the deal with `contacts=<ids>`.

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
An item may carry `"op"`. If it does, it must match the command. `add-note` items may carry `"on"`
(`contact`, `deal` or `account`), and `link-deal-contacts` items look like `{"id": 1, "add": [2, 3], "remove": [4]}`.

### Formats Freshsales changes on read-back

Verification treats these as matches: dates come back as `2026-11-01T00:00:00+00:00` for a saved `2026-11-01`,
and phone numbers come back with the spaces removed (`020 0000 0000` becomes `02000000000`). Both were found
by checking the real API. The diff also treats them as unchanged, so re-running a command shows no changes.

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
| Create | `POST contacts` / `POST deals` / `POST sales_accounts` `{"contact": {...}}`, read back with `GET <path>/{id}?include=...` |
| Deal contacts | `GET deals/{id}?include=contacts`, `PUT deals/{id}` `{"deal": {"contacts_added_list": [...], "contacts_removed_list": [...]}}` |
| Note | `POST notes` `{"note": {"description", "targetable_type": "Contact" or "Deal" or "SalesAccount", "targetable_id"}}`, read back with `GET <path>/{id}?include=notes` |

The docs don't say where included associations appear in the response (inside the record or at the
top level). The parser accepts both.

## Tests

```bash
python3 -m unittest -v
```
All HTTP is mocked (`tests/fakes.py`). The tests make no network calls.
