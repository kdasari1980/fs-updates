---
name: verify
description: Verify fs CLI changes end-to-end without touching live Freshsales — runs the real ./fs over HTTP against a local fake API.
---
# Verifying `fs`

Live writes are never allowed during development. Live reads need working auth
(`FS_AUTH_VIA_PROXY=1` + a network secret for tern.freshworks.com, or `FS_API_KEY`).
Try `./fs picklists` first; a 401 means auth isn't attached; use the fake below.

## Local fake (real CLI, real HTTP)
```bash
D=.claude/skills/verify; LOG=$(mktemp)
python3 $D/fake_fs.py 8765 $LOG &           # contact 11, deal 5, account 100
$D/fsl picklists                            # fsl = ./fs pointed at 127.0.0.1:8765
```
- `fsl` sets NO_PROXY for 127.0.0.1 (HTTPS_PROXY is set in cloud sessions) and cds to the
  repo root, so pass **absolute** paths for `--batch`.
- `$LOG` is JSONL of every request incl. the `Authorization` header: check it for "no
  writes on dry-run" and "no auth header in proxy mode".
- The fake silently ignores `city` on contact PUT (exercises DID NOT TAKE) and returns
  two 429s (Retry-After: 1) on the first `selector/deal_stages` calls.

## Flows worth driving
picklists, lookup (found + not found), find-deals Engines, account 100 / 999,
update-contact dry-run then --apply, invalid picklist with --apply (0 PUTs),
update-deal deal_stage=LOI --apply (429 retry), add-note dry/apply, --batch with one invalid item.

Creates: `create-contact --preset target-contact --set first_name=Zed --set email=zed@example.com` dry-run then
`--apply` (verified), the same again (duplicate blocks it), an email containing `ghost` (the fake drops
`subscription_types`, so verification reports DID NOT TAKE), `create-deal --preset target-deal --set name=X --set contacts=11 --apply`,
`create-account --preset advisor-account --set name=X --set 'cf_type=SO-CF;AD-A&T' --apply`. The fake keeps what POST creates (ids from 60).

Gotcha: `pkill -f fake_fs.py` from the same shell line can kill that shell (exit 144); use `kill %1`.

Step 2 flows: `add-note 5 "x" --on deal --apply`, `add-note 100 "x" --on account --apply`,
`link-deal-contacts 5 --add 12 --remove 11` dry-run then `--apply` (the fake appends and removes like the
live API; deal 5 starts with contact 11), `update-contact 11 --set cf_rereachout=Chase --set owner="Joanna Te"`.

Search flows (read-only): `views`, `views --entity deals`, `list-view 1 --where city~London`,
`find-contacts --name "Ada Lovelace"`, `find-contacts --name Love`, `find-contacts --city London --type ...`.
The fake only has contact 11 (Ada, London). `POST filtered_search/contact` is a read, so it may appear in the
request log on a dry-run.
