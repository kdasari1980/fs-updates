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

```bash
./fs find-contacts --name "Richard Boyden"        # "First Last" is exact; one word is part of the surname
./fs find-contacts --city "Reading,Oxford" --type SO-CF --limit 50
./fs find-contacts --email richard.boyden@frpadvisory.com
./fs find-contacts --domain frpadvisory.com       # on its own: free-text search, stops at 100 results
./fs find-contacts --city Reading --full          # each contact in full, with custom fields (one request each)
```
Server-side search. Filters are ANDed, matching ignores case, and commas mean any-of (`Reading,Oxford`). Names,
city and email match whole values (a single word searches part of the surname). `--type` is checked against the
Contact Type picklist. Results carry id, name, email, job title, city and country but **no custom fields**; use
`--full`, `lookup`, or `list-view` for those. Output says how many matched in total if more were returned than shown.

```bash
./fs views                                  # saved views; --entity deals | accounts
./fs list-view 13000351421 --where "cf_contact_type_newct=SO-CF" --where "city~Reading" --max-pages 5
./fs list-view 13000351421 --where domain=frpadvisory.com --fields id,display_name,cf_rereachout,cf_reachout_priority
```
Reads a saved view page by page (100 rows a request) and filters the rows here, so you can filter on **any field,
including custom fields**. `field=value` matches a whole value, `field~part` matches part of it, both ignore case,
commas mean any-of, and `domain=` matches the email domain exactly (secondary emails too). Default columns are
id, name, email, job title, city and the Contact Type, Reachout Status, Priority and Final Status fields. The report
says how many pages were read and warns if `--max-pages` stopped it early. Each page counts against the account's
1000 requests an hour, and the All Contacts view is about 74 pages.

```bash
./fs contact 70001234567,70001234568      # contacts in full: custom fields, accounts, deals (with stage ids), notes
./fs deal 70009876543                     # a deal in full: stage, owner, custom fields, contacts, notes
./fs lookup a@x.com,b@x.com --brief       # search only: no custom fields, one call per 25 emails
./fs find-deals "Acme,Beta" --details     # also fetch stage/amount for each hit (one call each, first 10 per term)
./fs list-view 13000351421 --where cf_contact_type_newct=SO-CF --stop-after 20
./fs check-contacts --view 13000351421 --where city~Reading --emit-batch fix.json
```
`find-deals` now returns just `{id, name}` for each hit (one call per term). It used to fetch every hit in full, and a single
search can return 100 hits, so one term could cost over 100 calls.

`check-contacts` reports Tern's rules (reads only): SO-CF contacts need a priority of P1 or P3 and P3 means Reachout Status
`NA/Not Relevant`; every contact needs a city, a LinkedIn profile and an email. It prints the counts the skill asks for and
lists each contact's problems. `--view` is the cheapest source (100 contacts per call); `--ids` costs a call per contact. The one
mechanical fix, P3 status, can be written to a batch file with `--emit-batch` for `update-contact` (the old status it would replace is
recorded in `replaces`, because replacing it overwrites outreach history). Missing priorities are never guessed: they are questions
for Karthik. The same rules also show as `WARNING` lines in the dry-run of `update-contact` and `create-contact`; warnings never block.

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
there is no match or more than one match), `email`, `subscription_types` (names or ids, `;` separated), and **any `cf_*` custom field**. Custom fields are checked by type:
dropdowns against their choices, multi-selects value by value (`A;B`, order doesn't matter), checkboxes `true`/`false`,
numbers, `YYYY-MM-DD` dates and free text. `auto_complete` fields (such as `cf_reports_to`) are rejected.

`email=new@x.com` makes the new address primary and keeps every old address as a secondary one. Freshsales replaces the whole
list on a PUT (anything left out is deleted), so the command sends them all back. An address that already belongs to another contact
is refused with the `merge-contacts` command to use (this happens when an email BCC created a blank duplicate). The contact's
account link stays attached.
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

```bash
./fs update-account 70005555555 --set name="Debrett's" --set "cf_type=SO-CF;AD-A&T" --set city=London --set owner="Karthik Dasari"
```
Supported account keys: `name` (refused if another account already has that name), `website`, `phone`, `address`, `city`, `state`,
`zipcode`, `country`, `owner`, and any `cf_*` field (Account Type is a multi-select, so use `;`). Verified from the PUT's own response.

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

### Delete and merge (permanent)

```bash
./fs delete-contact 70001234567 70001234568          # dry-run: shows what would go and what blocks it
./fs delete-contact 70001234567 --apply
./fs delete-deal 70009876543 --apply
./fs delete-account 70005555555 --apply
./fs merge-contacts 70001234567 --with 70001234999,70001235000          # dry-run preview
./fs merge-contacts 70001234567 --with 70001234999 --apply
```
**Guards**, because neither can be undone through the API (there is no restore):
* a **contact** is refused if it is on a **live deal** or has **notes**. A deal is live unless it is in Screened Out (18), Rejected by Tern (64, 68),
  Rejected by Seller (72), Sold to Other (76) or Sold to Tern (80); "Future-Followup" and "No Response" count as live, and so does a deal whose stage can't be
  read. Notes count whether they are on the contact or on one of its deals. The way past the live-deal guard is explicit: unlink the contact with
  `link-deal-contacts <deal> --remove <contact>`, then delete it. Notes block with no override, because they are history;
* a **deal** is refused if it has **notes**. Its contacts stay in Freshsales and are only unlinked;
* an **account** is refused while it still has **contacts or deals**;
* there is **no "forget"** (hard delete) and never the "also delete the contacts and deals" option;
* a batch is all-or-nothing: if any id is blocked, nothing is deleted.

The dry-run shows the record, its deals with their stage (live ones marked), and its notes. `--apply` sends the DELETE, trusts the `true` it
answers (no read-back call), and prints what was deleted in the JSON (`result.deleted`), since it can't be recovered.

**Merge** (`POST contacts/merge`, found by probing; it isn't in the public docs). What a live test showed: the primary keeps every value it
already has and takes the duplicate's value only where its own is blank (a city and a priority moved across, a job title and a notes field did not);
the duplicate's email becomes a non-primary address; its notes and deal links move to the primary; the duplicate is deleted afterwards.
The preview lists each of those, and the values that will be dropped. Freshsales does the work in the background, so after the POST the
command checks the response and, if anything is still missing, re-reads the primary a few times (2, 3, 3, 4 and 4 seconds apart, one call
each) until every address, note and deal link is there. If it is still unfinished the status is `PENDING` (exit 1): check again with `fs contact <id>`.

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

## API call budget

Freshsales limits the whole account, and everything shares the pool: this CLI, Zapier, and anyone logged in with an API key.
What was measured on the live API:
* **every request costs exactly one call**, whatever the endpoint, the page size or the `include`. A 100-row view page costs the same as one record, so
  one call that returns more is always cheaper than several that return less;
* the response headers say the window is **2000 calls an hour** (the docs say 1000), with a separate limit of **400 a minute**, and give the time it
  refills. Another user or automation can spend the pool while you work: in one test run about 900 calls disappeared in a few minutes.

So each run ends with a line on stderr: `API calls this run: 4 (1 writes); account has 1209 of 2000 left in this window, refilling at 09:59 UTC`.
Retries after a 429 are counted. A sweep (`list-view`, `check-contacts --view`) stops early when fewer than 100 calls are left, and `--apply`
refuses to start a batch that the remaining calls can't cover (keeping 50 spare).

**What keeps the count down:**
| Technique | Saves |
|---|---|
| Field, picklist, deal-stage and saved-view definitions are cached on disk for an hour (`~/.cache/fs-cli`, `FS_CACHE_TTL`, `FS_NO_CACHE=1`, `fs --refresh` to refetch). Only metadata is stored, never records or the key. Errors from an old cache say so | 1-3 calls per command |
| A write is sent with `?include=` so its response is the saved record, and that is what gets verified. A second call happens only if the response lacks something the checks need | 1 call per write |
| The same GET is sent once per run (any write clears the remembered copies, so verification is never stale) | repeat reads |
| One read per record carries everything: a contact comes with its accounts, owner, deals and notes, a deal with its contacts and notes | 1-2 calls per command |
| A batch of new contacts checks for duplicate emails with one search per 25 emails | N-1 calls per batch |
| `lookup` finds many emails with one search per 25, then reads each found contact once (`--brief` skips that) | about half |
| `find-deals` returns hits only unless `--details` is given | up to 100 per term |
| Links, notes and deletes send only what changes; notes and deletes are verified from their own response | 1 call each |

**Typical costs** (with a warm metadata cache; add 1-3 the first time in an hour):
| Command | Dry-run | `--apply` (plans again, then writes) |
|---|---|---|
| `update-contact` / `update-deal` (one record) | 1 | 2 |
| `create-contact` (one) | 1-2 | 2-3 |
| `create-contact --batch` of N | 1 + N/25 | 1 + N/25 + N |
| `create-deal` with 2 contacts | 3-4 | 4-5 |
| `add-note` | 1 | 2 |
| `link-deal-contacts` | 1-2 | 2-3 |
| `delete-contact` / `delete-deal` / `delete-account` | 1 | 2 |
| `merge-contacts` with one duplicate | 2 | 3, plus a re-read per poll if unfinished |
| `lookup` of K emails | 1 + K/25 + found | same (read only) |
| `list-view` / `check-contacts --view` | 1 per 100 rows read | same (read only) |

The dry-run then apply routine plans twice by design, because the second run re-reads each record so the change is never made over a
stale diff. If a run still needs trimming: use `--brief`, `--stop-after`, `--max-pages`, and batches.

On HTTP 429 the client retries up to 3 times. It honours `Retry-After` when that is 120s or less, and otherwise backs off 2s, 4s, 8s. If `Retry-After` is longer than 120s, it fails immediately.

## Not supported

* **"Forget"** (hard delete of a record and all its data) and deleting an account together with its contacts and deals. Both are deliberately absent.
* Merging deals or accounts (the API has a route for contacts only).
* Restoring anything that was deleted: the API has no restore for contacts, deals or accounts.

## Endpoints used

Checked against https://developers.freshworks.com/crm/api/:

| Purpose | Call |
|---|---|
| Email lookup | `GET lookup?q=<email>&f=email&entities=contact` |
| Many emails at once | `POST filtered_search/contact` with `contact_email.email` `is_in` and a comma-separated value |
| Write with the result | `POST`/`PUT <path>?include=...` returns the saved record with the same extras as a GET |
| Contact search | `POST filtered_search/contact?per_page=100` `{"filter_rule": [{"attribute", "operator": "is_in" or "contains", "value"}]}` (a read, ANDed; `contains` works on `last_name` only); domain via `GET search?q=<domain>&include=contact` |
| Saved views | `GET {contacts,deals,sales_accounts}/filters`, `GET <path>/view/{id}?page=&per_page=100` (whole records incl. `custom_field`) |
| Contact | `GET contacts/{id}?include=sales_accounts`, `PUT contacts/{id}` `{"contact": {..., "custom_field": {...}, "sales_accounts": [{"id", "is_primary"}]}}` |
| Search | `GET search?q=<term>&include=deal,sales_account&per_page=100` |
| Deal | `GET deals/{id}?include=sales_account,deal_stage`, `PUT deals/{id}` `{"deal": {...}}` |
| Account | `GET sales_accounts/{id}?include=contacts,deals` |
| Fields/picklists | `GET settings/{contacts,sales_accounts,deals}/fields` (`choices[].value`) |
| Deal stages | `GET selector/deal_stages` |
| Create | `POST contacts` / `POST deals` / `POST sales_accounts` `{"contact": {...}}`, read back with `GET <path>/{id}?include=...` |
| Deal contacts | `GET deals/{id}?include=contacts`, `PUT deals/{id}` `{"deal": {"contacts_added_list": [...], "contacts_removed_list": [...]}}` |
| Delete | `DELETE contacts/{id}` / `deals/{id}` / `sales_accounts/{id}` (answers `true`) |
| Merge | `POST contacts/merge` `{"primary_id", "secondary_ids": [...]}`; background job |
| Note | `POST notes` `{"note": {"description", "targetable_type": "Contact" or "Deal" or "SalesAccount", "targetable_id"}}`, read back with `GET <path>/{id}?include=notes` |

The docs don't say where included associations appear in the response (inside the record or at the
top level). The parser accepts both.

## Tests

```bash
python3 -m unittest -v
```
All HTTP is mocked (`tests/fakes.py`). The tests make no network calls.
