---
name: fs-update
description: Handle any update to Tern's Freshsales from a list of contacts, deals or accounts: add or enrich contacts, create deals or advisor accounts, link, merge, rename, reopen, delete. Uses the `fs` CLI (the fs-updates repo), which calls the Freshsales API directly, with no Zapier. Supersedes contact-add, contact-update, deal-add and the Zapier version of fs-update.
---

# fs-update

One router skill for every change to Tern Capital's Freshsales (tern.freshworks.com), made with the `fs` CLI in the `fs-updates` repo (`./fs`, run from its checkout; see its README). Thread-based status updates (the FSU label) stay with fs-updates-from-email.

Input can be: a pasted list or free text, a CSV/Excel/Google Sheet, a screenshot of a list, or free-form edit instructions on existing records (rename, merge, set a field, change owner or stage, delete). Each item is a contact (has an email or a person's name), a deal or company name, or an account name.

Working style: British English, direct. Propose a short structured plan in text first, get ONE confirmation per batch, then execute and verify. Write structured text before any table or CSV.

## 0. How the CLI works (read this once per session)

- **Every write is a dry run until `--apply`.** The dry run prints a diff (`before / after`, with `OVERWRITE`, `default`, `DELETE` and `MERGE` flags), `WARNING` lines and `INVALID` lines. Show it, wait for the single confirmation for the batch, then re-run with `--apply`. `--apply` re-validates, sends, and prints a `field / intended / actual` table; any `DID NOT TAKE` is a failure to report. One INVALID item stops the whole batch from being sent.
- **Exception to "wait for confirmation":** when Karthik gives a numbered list of explicit per-record instructions (delete X, move Y, add Z), that list is the confirmation: show each dry run and apply straight away, then report. The delete and merge guards below still apply.
- First write of a session: run `./fs picklists` (and read any `INVALID ... Valid: [...]` list), because Freshsales silently ignores an invalid picklist value. The CLI already refuses them.
- Auth: in a cloud session `FS_AUTH_VIA_PROXY=1` with the tern.freshworks.com network secret; elsewhere `FS_API_KEY` in the environment or the gitignored `.env`. Never print or commit the key.
- **API budget.** Freshsales allows about 2000 calls an hour for the WHOLE account (Zapier and other users share it) and each request costs one call, whatever its size. Every run ends with `API calls this run: N ... account has X of 2000 left`. Prefer one call that returns more: `lookup ... --brief`, `list-view` and `check-contacts --view` (100 contacts a call), `--stop-after`, batches (`--batch FILE`), and `find-deals` without `--details`. Never loop `lookup` per email or read contacts one by one when a view or batch will do. If the line shows the pool is low, tell Karthik and stop sweeping.
- Metadata (fields, picklists, stages, views) is cached for an hour; `./fs --refresh ...` refetches it if Freshsales' picklists were just changed.
- Reads: `lookup EMAILS [--brief]`, `find-contacts [--name|--email|--city|--type|--domain] [--full]`, `contact IDS`, `deal IDS`, `account ID`, `find-deals TERMS [--details]`, `views [--entity deals|accounts]`, `list-view VIEW_ID --where ... --fields ...`, `check-contacts`, `picklists`.
- Writes: `create-contact|create-deal|create-account [--preset P] --set k=v ...`, `update-contact|update-deal|update-account ID --set k=v ...`, `add-note ID TEXT --on contact|deal|account`, `link-deal-contacts DEAL --add IDS --remove IDS`, `delete-contact|delete-deal|delete-account IDS`, `merge-contacts PRIMARY --with IDS`.
- Presets carry the defaults below and show as `default` in the diff: `target-contact`, `advisor-contact`, `target-deal`, `advisor-account`. Any `--set` overrides them. Multi-selects (such as Account Type) take `;`-joined values: `--set "cf_type=SO-CF;AD-A&T"`. `--set key=` (empty) clears a field. Choice fields (`owner`, `stage`, ...) take the name.
- Tools that are not Freshsales stay as before: Apollo, Superhuman, Google Sheets, Exa/Companies House.

## 0a. Standing rules

- Defaults (state them in the plan so Karthik can override): owner Karthik Dasari, Deal Source Direct, Bolt-on Yes, Bolt-on Platform Other. When linking contacts to an EXISTING deal owned by Simon (Bolster, Ticketline, Oct 2026), leave deal and contact owner as Simon and say so.
- Plan first, one confirm per batch. Anything ambiguous gets a multiple-choice question with my best guess first.
- Write failure or dropped connection: retry once (first check the record does not already exist, never create a duplicate; note search lags new records by a few minutes, so look a second time with `contact ID` if you hold the id), carry on with the batch, list failures with the error at the end.
- Apollo: free people search and standard match (about 1 credit) by default. `apollo_mixed_people_api_search` is free but returns obfuscated surnames and no emails; take the Apollo `id` from it and pass the id to `apollo_people_bulk_match` (about 1 credit each, max 10 per call). Ask before any waterfall email/phone (about 10 credits each). Always surface the `mcp_credits` block and the balance.
- Explicit instructions from Karthik win over my judgement (do as told, flag conflicts such as a future GTM date). Exception: a company he names that fails the fit screen still gets screened (section 3).
- Never expose these as guesses: Contact Type, reachout statuses. Infer from evidence and show the choice in the plan.
- **Apollo-verified email beats the Freshsales address (Oct 2026).** Freshsales addresses are often guesses. In the Manchester outreach batch Apollo corrected six addresses and showed one contact (Bolster's Joseph Bramwell) now working at a different company; earlier examples were MiTEQ (awhiting@ bounced, alex.whiting@ verified) and KPR (d.marla@ bounced, d.clapp@ correct). When enriching or linking a contact, run an Apollo match, compare with the FS primary email, and show any mismatch in the plan. Change it only after approval with `update-contact ID --set email=new@x.com`: the new address becomes primary and every old address stays as a secondary (the CLI sends them all back, because Freshsales deletes any address left out of an update). This matters because outreach BCCs to dasari+5yzvble@tern.freshworks.com only log against the contact whose FS email matches the address emailed. The CLI refuses an address that already belongs to another contact and names the `merge-contacts` command to use.
- **Do not touch a company or contact that belongs to a live deal or due diligence without asking (Oct 2026).** Before editing, merging or re-staging a record whose name or email domain resembles a business Tern is in discussions or DD with (for example CODAS's contact at cds-systems.co.uk versus Corporate Data Services, under exclusivity, and not Curium Data Systems), read the matching memory file or the FS deal, confirm they are different entities, and flag it. `delete-contact` enforces its own version of this (section 8); edits and merges do not, so the check is yours.

## 0b. SO-CF priority and status rules (Karthik, Oct 2026)

These apply whenever an update involves a contact whose Contact Type is `SO-CF`, and to SO-CF contacts only (not SO-IP, AD-*, TG-*, TL-* or any other type). Scope is the contacts in the batch being updated, not a sweep of the whole database, unless Karthik asks for a sweep (he has asked for a Manchester-area check only, never the full database).

1. **Priority is P1 or P3 only.** Every SO-CF contact in the update must end with `cf_reachout_priority` P1 or P3. Existing P2 values on SO-CF contacts that are in the batch should be flagged and put to Karthik (ask P1 or P3), not left silently.
2. **P3 means NA/Not Relevant.** Every SO-CF contact with priority P3 must have Reachout Status (`cf_rereachout`) = `NA/Not Relevant`. If it has any other status or none, update the status to `NA/Not Relevant` and say what it replaced in the report (note it overwrites any outreach history such as 1st Contact-Email/LI or Meeting Done VC, so list the old values before changing them).
3. **No priority: ask.** If an SO-CF contact in the batch has no priority, do not guess. Ask Karthik which priority (P1 or P3) per contact, grouped in one multiple-choice question with my best guess first. Once he answers: P1 set as told; P3 set P3 and then set Reachout Status to `NA/Not Relevant` per rule 2.
4. Contacts on the Manchester Google Sheet (CF Meeting Master, Advisors tab) are P1 by Karthik's earlier instruction; keep them P1.
5. Include a short check line in the plan and the report: how many SO-CF contacts were checked, how many had no priority, how many P3s had the status corrected.

How: the dry run of every `update-contact` and `create-contact` prints a `WARNING` for any SO-CF breach in the state it would leave, so none slips through. For a whole batch or view use `./fs check-contacts --ids ... | --emails ... | --view VIEW_ID --where ...`: it prints the exact counts for item 5 and lists each contact's problems. Its `--emit-batch fix.json` writes ONLY the mechanical fix (P3 status to `NA/Not Relevant`, with the old status recorded in `replaces`); list those old values to Karthik, then `./fs update-contact --batch fix.json` (dry run), then `--apply`. Set priorities with `--set cf_reachout_priority=P1` once he has chosen.

## 0c. Every contact being updated needs City, LinkedIn and latest email (Karthik, Oct 2026)

Any contact that is created or updated in a batch (any Contact Type, not only SO-CF) must finish with all three populated and current:

1. **City** (`city`).
2. **LinkedIn profile** (`linkedin`, slug only).
3. **Latest email address** (the current work address, as primary).

How:

- Check at the start of the batch: `./fs check-contacts --ids ...` (or `contact IDS` for the full record) lists what is missing; the dry run also warns (`city is missing`, `LinkedIn is missing`, `email is missing`). List the gaps in the plan.
- Use Apollo for the LinkedIn profile and the latest email: standard `apollo_people_match` (about 1 credit) using name plus company or domain, or the LinkedIn URL if known; for several people, free `apollo_mixed_people_api_search` then `apollo_people_bulk_match` by id (max 10 per call). Check `apollo_users_api_profile` (include_waterfall_capability) once per conversation. Ask before any waterfall.
- City: use the email signature city where there is one, else the Apollo person city (note it may be stale, see section 4); say which source was used.
- Where Apollo's email differs from the FS primary, show the mismatch in the plan and change it only after approval (`update-contact ID --set email=...`). Where Apollo has a different employer or no confident match, flag it and do not apply.
- Contacts that are only being deleted or merged away are exempt; the surviving record gets checked.
- Report line: how many contacts were checked, how many had a missing or changed city, LinkedIn or email, what Apollo filled, and the credits used and balance. List any that Apollo could not resolve.

## 1. Classify each input

1. Contact with an email: look it up first. `./fs lookup a@x.com,b@x.com` (one search for up to 25 emails; add `--brief` when you only need "exists or not", which skips reading each contact). If the exact email is not found but a near match exists (same name or domain), show the mapping and ask before changing anything: `./fs find-contacts --name "First Last"` (or one word for a partial surname) and `--domain example.com`. **If a name search returns nothing, suspect a misspelling** (Karthik wrote "Sokrataus"; the record was Peter Sokratous): find the deal with `./fs find-deals "stem,variant"`, read its contacts with `./fs deal ID`, and confirm the match with him before deleting or editing. A name that is simply not in FS (Tom Palmer at Rothschild, Oct 2026) is reported as "not in FS, nothing to delete", not an error. `find-contacts --domain` stops at 100 results (it says so); for a complete domain list use `list-view VIEW --where domain=example.com`.
2. Contact without an email: free Apollo search plus a standard match to find one. If still none, ask: waterfall, create with LinkedIn/mobile only (`create-contact ... --set mobile_number=...` with no email; the CLI accepts an email OR a mobile), or skip.
3. Deal or company name only: run the full target flow (sections 3 to 6) including finding the shareholder and CEO.
4. Account name only: decide by what the firm is. Advisor firm: find or create the Account. Software target: create a Deal instead (never an Account). Unclear: ask.
5. Free-form edit instruction: apply as written (section 8).

Then decide what the contact is by researching (website, Apollo organisation, Companies House). If still unclear, ask.

| Contact is | Handling |
|---|---|
| Owner/manager of a software target | Deal plus linked contact (sections 3 to 6). Account is always Not Qualified. |
| Corporate finance, M&A, accountancy or other advisor | Account for the firm plus contact under it (section 7). |
| PE acquirer, search fund, investor, NED/chair, lender, lawyer, other | Always ask what to do (type, whether to create an Account). |
| Email domain on the Tern portfolio list | Always ask for type, account and subscription each time. Domains: terncapital.com, novevasoftwaregroup.com, mindofmyown.org.uk, thomsonscreening.com, sofco.co.uk, metrofy.com, cadesignservices.co.uk, pimss.com, retail-manager.com, metabroadcast.com, prosper-design.com, charitylog.co.uk, ccube.cloud. Telsis and Telency are NOT portfolio. |

Personal addresses (Gmail etc.): look up the person on LinkedIn/Apollo for the company; if you cannot find it, ask. Do not create a deal for an unknown company.

**Sheet-sourced lists (Oct 2026).** When the list comes from a Google Sheet (for example the Owners tab of a deal-review sheet), read it with the Sheets connector and re-read it immediately before writing if anyone could have re-sorted it, because row numbers move while the notes travel with the rows. Match on company and contact name, not row number. Respect human-entered values in reject-reason and notes columns: append to notes, never overwrite.

## 2. Pre-checks for every target (in this order, cheapest first)

1. **FS duplicate check.** `./fs find-deals "stem,variant1,variant2"` (comma-separated, one call per term, up to about 5 terms; hits come back as `deals` and `sales_accounts` separately, so an empty account named like the company is a stray duplicate account, NOT a second deal). Search the distinctive name stem and variants (hyphens, spaces, legal vs trading name, brand vs company). Add `--details` only when you need stage or amount (one extra call per hit, first 10 per term). Two deals for one company: merge them by hand (section 8). **Same-name companies:** if the name is shared by unrelated businesses (Ticketline Manchester vs Ticketline Canada, Oct 2026), confirm which entity Karthik means (location, website, Companies House) before researching owners or linking anything. `create-deal` and `create-account` also refuse an exact duplicate name and warn about look-alikes (they strip trailing Ltd/Group), but the search lags records created in the last few minutes.
2. **Prior-contact check, always for targets.** FS deal owner, last contacted, activity and notes (`./fs deal ID` shows the deal's notes and contacts; `./fs contact ID` shows a contact's notes and deals); plus Superhuman `list_threads` by the company domain and by each contact email. Report who contacted them, whether they replied and the gist. Informational; does not block. A prior reachout status such as "3rd Contact-Email/LI" means a re-approach: say so (Clarus was treated this way in the Manchester batch).
3. **Fit screen** (section 3).

### Existing deal found
- Link the contact to it automatically: `./fs link-deal-contacts DEAL_ID --add CONTACT_ID`. It APPENDS (ids already linked are harmless), so there is nothing to read first, and it never replaces the list. Use `--remove` to unlink. The deal's contacts are read back and must equal the intended set.
- Do NOT reopen automatically. If the stage is 06, 52, 18, 68, 72 or 76, link the contact, report the stage and leave it. Karthik reopens by instruction: `./fs update-deal ID --set stage="04 Plan-Teaser/Suspect" --set cf_deal_stagelatest="10 Suspect" --set owner="Karthik Dasari" --set cf_chaser_status=Chase`, plus a dated note holding the old history (`add-note ID "..." --on deal`).
- Contact owner follows deal owner; say so in the report.
- **Replacing a contact (Oct 2026):** when Karthik says to add new contacts and remove an old one (Bolster: three owner/directors added, Joseph Bramwell removed; Ticketline: Peter Sokratous removed), add and link the new contacts first, verify the links, then unlink and delete the old contact (`link-deal-contacts DEAL --remove OLD`, then `delete-contact OLD`; the guards in section 8 apply). Keep exactly one primary contact on the deal.

## 3. Fit screen

Work out what the company does from its website. Tests: (1) ownership/independence via Companies House PSC (follow corporate PSCs up the chain; a minority VC/PE stake with founders in control is a judgement call, show the cap table); (2) vertical software, not horizontal; (3) UK or Ireland HQ; (4) size c. £0.5-3m ARR. Unknown size is NOT a reject: proceed and flag it.

- **Horizontal (HMS) test (Karthik, 15 Oct 2026):** if the product works the same across sectors and the customer is defined by an IT or back-office function rather than an industry, it is horizontal market software, not vertical (VMS). Interlink Software (vendor-neutral AIOps/IT monitoring layered over other tools, sold to banks, payments and infrastructure operators) is the reference example. Reject reason `Tern-HMS`.
- **Hardware (Karthik, 15 Oct 2026):** a hardware business, or hardware-led with software as an add-on (ICC Solutions), is not the right fit. Reject reason `Tern-Not Right Fit`.
- **Clearly not software** (reseller, agency, recruitment, property, manufacturing with no product, and no history in FS): never create it. If it already exists in FS with no outreach or notes of value, delete it and report exactly what went (IDs, what was linked). `delete-deal` refuses a deal that has notes; its contacts stay and are only unlinked, so delete the deal first, then each contact (`delete-contact` refuses a contact still on a live deal or with notes, which is the contact protection in section 8). The dry run lists what goes. Borderline (tech-enabled services, unclear): ask first.
- **Karthik says "reject", "not the right fit" or "screen out" on an existing deal:** do NOT delete. Move it to Screened Out with the matching reject reason and a dated note: `./fs update-deal ID --set stage="18 Screened Out" --set cf_deal_stagelatest="05 Screened Out" --set cf_reject_reason="Tern-Not Right Fit"` (or `Tern-HMS` for horizontal) and `add-note ID "..." --on deal`. If he also says to remove the contact, delete it after the contact-protection check. If outreach was already sent to the contact, use stage `68 Rejected-By Tern-Notified` instead and say so; if none was sent, stage 18 (Workstars Global, Oct 2026: no outreach sent, so stage 18, reject reason Tern-Not Right Fit, contact Jason Harney deleted). Then tell him any unsent Superhuman draft to that contact is stale and ask whether to discard it.
- **Other fit failures** (PE/VC-owned, parent-owned, listed, non-UKI, horizontal, too small or large) for a company not yet in FS: create the deal as Screened Out with the right reject reason, no contacts, no Apollo spend: `./fs create-deal --preset target-deal --set name="..." --set stage="18 Screened Out" --set cf_deal_stagelatest="05 Screened Out" --set cf_reject_reason="Tern-OS Large PE"`. This applies even when Karthik names the company. Include a one-line reason with sources in the deal note.

Reject reasons (`cf_reject_reason`): `Tern-OS Large PE`, `Tern-OS Large Parent`, `Tern-OS Public`, `Seller-Sold`, `Tern-Non UKI Location`, `Tern-HMS` or `Tern-Not Right Fit` (horizontal), `Tern-Rev Small`, `Tern-Rev Large`, `Tern-Reseller/Managed Services/Not Software` (only if keeping a record). The CLI checks the value against the live list.

Industry (`cf_industry`, best single value): Healthcare/Public Sector, Public Sector, Retail, Transportation, Real Estate, Hospitality, Industry Agnostic, Education, Energy & Utilities, Financial Services, Manufacturing, Media, Other/Unknown (last resort), Professional Services, Telecoms, Compliance, Niche ERP.

## 4. Find people for a target

Only for live Suspects where Karthik gave no contact (or asked for the owners, as with Bolster and Ticketline). Shareholder from the Companies House PSC register (individual with the largest holding; follow the chain up through corporate PSCs: Ticketline resolved through Betesh Management Ltd and Orbit Tickets Group Ltd), and CEO via `apollo_mixed_people_api_search` (domain first, then keywords with UK location). Companies House pages can be read through Exa `web_fetch_exa`. Dedupe against FS first (`lookup ... --brief`) and reuse existing contact ids. Show roles, evidence and credit cost, and get approval before any waterfall. A contact needs an email or a mobile.

**Waterfall results can be poor.** For the Ticketline owners the waterfall returned invalid or abuse-flagged addresses (about 5 credits). When the results are weak, show them and ask: add anyway flagged as unverified, or skip. If Karthik chooses to add, create the contact with the best address, say in the contact and deal notes that the email is unverified or guessed, and do not spend more credits. Other contacts with no email at all (Linda Betesh) are not added.

Check the contact's own location and title as well as the company's: Apollo person city and title come from LinkedIn and can be stale (the Manchester batch found contacts in Toronto and Edinburgh for Manchester-area companies, and a contact who had moved company). Use the signature city where there is one and note the source.

## 5. Create target contacts

`./fs create-contact --preset target-contact --set first_name=... --set last_name=... --set email=... --set job_title=... --set linkedin=SLUG --set mobile_number=... --set city=... --set country=... --set cf_primary_contact=true ...`

The preset sets: owner Karthik Dasari (or the deal owner, see 0a: `--set owner=...`), lifecycle Lead with status Unqualified Leads, Contact Type `TG-Owner/Manager` (all target contacts, including hired CEOs; never TL-* for target people), `cf_reachout_final_statustargets` Unqualified, `cf_rereachout` Chase, subscription Deal Announcements only, and the account Not Qualified (primary). All of that is set in the one create call and checked from its response. Also set, from Apollo: `cf_apollo_person_id`, `cf_seniority`, `cf_email_status`, `cf_linkedin_headline`, and Apollo's company fields as available. `cf_primary_contact=true` on exactly ONE contact per deal (shareholder/owner first, else CEO/MD; keep an existing primary). Job title from Apollo when match confidence is high; LinkedIn slug only; city from the signature where there is one.

Link to the deal with `create-deal ... --set contacts=ID1,ID2` (new deal) or `link-deal-contacts DEAL --add ID` (existing deal), then run `./fs contact ID` for each if you want to confirm the Not Qualified account and subscription survived the link.

Use the name the person actually goes by (Dave rather than David) in the first-name field only if the company site or LinkedIn confirms it; otherwise keep the legal name and note the preferred name in the deal note so outreach greets them correctly.

Existing contact with gaps: fill blanks from high-confidence Apollo or signature data with `update-contact` and fix clear errors (periods or initials in names, `-1` titles, garbled characters, email-as-name). List conflicts with non-empty values for Karthik (the dry run flags every `OVERWRITE`). Apollo match confidence low with empty employment history is not safe to apply. Existing target contacts with subscription `1;2;3;4;5` or the wrong account get fixed automatically (`--set subscription_types="Deal Announcements" --set sales_account="Not Qualified"`); advisors do not.

Clearing a field: `--set job_title=` (works for standard and custom fields).

## 6. Create the deal

`./fs create-deal --preset target-deal --set name="..." --set contacts=ID1,ID2 --set cf_website=https://domain --set cf_industry="..." ...`

The preset sets: amount -1 (unknown), owner Karthik Dasari, pipeline All Tern Deals, stage `04 Plan-Teaser/Suspect`, `cf_deal_stagelatest` `10 Suspect`, `cf_chaser_status` Chase, `cf_source_deal` Direct (`Intermediary` only if told, then link the advisor contact after confirming the match), `cf_status` Healthy, `cf_deal_type` Standalone, Bolt-on Yes, Bolt-on Platform Other, currency GBP and `cf_deal_currency` GBP.
- Name: trading name as on the website, no Ltd or Limited (keep a suffix only to disambiguate).
- Website as `https://domain` in `cf_website`; Industry from section 3.
- Headcount (`cf_headcount`) only when blank and from Apollo or the website; flag stale values (do not overwrite). Never fill revenue or ARR (`cf_rev_ltm`, `cf_ea_ltm`, `cf_arr`) unless supplied.
- Write a dated evidence note on every deal created or changed: `./fs add-note DEAL_ID "DD/MM: ..." --on deal`. It holds ownership evidence, prior-contact result, fit judgement and flags, reopen history, what was changed, and any unverified emails.

The create call verifies every field from its own response, including the contact links, and prints the deal's link. Report `https://tern.freshworks.com/crm/sales/deals/ID`.

## 7. Advisor contacts and accounts

1. Find the firm's Account: `./fs find-deals "Firm name,variant"` (the `sales_accounts` list) and `./fs account ACCOUNT_ID` (the account with all its contacts and deals, which also shows who is already there). Firms rebrand and merge; link to the existing account rather than creating a duplicate and say which was used. **Group overlaps (Oct 2026):** a firm may now sit inside a larger group already in FS (Kay Johnson Gee is part of Xeinadin; Moore & Smalley is part of MHA). Check the group account (Xeinadin Group account 113000455575) and say when two contacts for "different" firms are really at the same group. For outreach, Karthik wants ONE email per group, to the most senior contact (Xeinadin: Alex White, Managing Partner North West, chosen over Paul Whitney, Head of CF). Keep the other contact in FS unless told to delete.
2. If none: `./fs create-account --preset advisor-account --set name="Exact Firm Name" --set "cf_type=SO-CF" --set website=domain --set city=... --set country="United Kingdom"`. Account Type values look like `SO-CF` or `AD-A&T;SO-CF` (`;` between them, not a comma). Use the firm name exactly as Karthik gives it, and offer a rename if Apollo spells it differently (Debrett vs Debrett's, Oct 2026); rename with `./fs update-account ID --set name="..."` (it refuses a name another account already has).
3. Contact Type: infer from the firm and show it for approval. Corporate finance or M&A adviser SO-CF; accountancy AD-A&T (plus SO-CF if they do M&A); law firm AD-Legal Employment, General, IP or Restructuring; other AD-* or SO-* by service. **There is no `TL-NED` option in FS** (Oct 2026): for a NED or non-executive use the closest, `TL-NonExec`, and tell Karthik. Valid TL options are TL-CCO/CEO/CFO/COO/CRO/CSO/CTO, TL-NonExec, TL-Other, TL-Tern Principals. The CLI rejects anything else and prints the full list.
4. Statuses: cold add = Chase plus Unqualified (the `advisor-contact` preset). If an email thread shows an invite sent, Meeting Setup plus Active Discussion; if he is actively corresponding, Active Discussion. Otherwise ask. After Karthik confirms an advisor email has been sent, set Reachout Status `1st Contact-Email/LI` (and the priority per 0b).
5. Create the advisor contact: `./fs create-contact --preset advisor-contact --set first_name=... --set last_name=... --set email=... --set job_title=... --set city=... --set linkedin=SLUG --set sales_account="Exact Account Name" --set cf_contact_type_newct=SO-CF --set cf_reachout_priority=P1 --set cf_commentsnext_steps="why added; email source (Apollo-verified or pattern guess)"`. Search for the person first (step 1 of section 1). The account is linked as primary in the same call. `sales_account=` needs the EXACT existing name; if the account was created in the last few minutes the search may not see it yet, so wait and retry. Then apply 0b and 0c.
6. Move an advisor to a new firm or address (Alex Kelly to MHA, Matt Beckley to DJH): `./fs update-contact ID --set email=new@firm.com --set sales_account="New Firm" --set job_title=... --set city=... --set cf_commentsnext_steps="was at Old Firm, old@x.com"`. The old address stays as a secondary and the dry run shows it. If the new firm has no Account in FS, create it first (step 2).
7. **Replacing an advisor Karthik tells me to drop:** find the best or most senior CF contact at the firm (Apollo free search by domain, seniority partner/director, then `apollo_people_bulk_match` by id for the verified email), check FS and the Reachout Status first (`lookup EMAIL`), create them, then delete the old contact (section 8). If the firm turns out to be part of a group already in the batch, flag the overlap and keep only the more senior person for outreach.
8. **BCC duplicates (Oct 2026):** sending to an address FS does not hold makes the BCC create a blank duplicate contact. If `update-contact --set email=` then refuses with "already belongs to contact N", merge the blank duplicate into the real contact: `./fs merge-contacts REAL_ID --with BLANK_ID` (dry run first), then retry.

## 8. Edits, merges, reopens and deletes

- Free-form edits (rename, owner, stage, field values, notes): do as instructed (`update-contact`, `update-deal`, `add-note`), verify from the table, add a dated note. Flag conflicts (e.g. a future GTM date) but do not block.
- **Merge** duplicate deals: keep the richer record (the one with contacts and most data, or the one Karthik names, e.g. the P3 record), carry over useful comments/fields from the other (append to `cf_comments__next_steps_upto_2_lines`, and state conflicts such as differing GTM date or revenue rather than silently choosing), move contacts across (`link-deal-contacts KEEP --add ...`, then `--remove` on the twin), list what moved, and delete the empty twin only when `delete-deal` allows it (it has no notes). There is no deal-merge command.
- **Merge** duplicate contacts: `./fs merge-contacts KEEP_ID --with DUP_ID[,DUP_ID]`. The dry run lists what will move (emails as secondary, notes, deal links) and what is filled or dropped: the kept contact keeps every value it already has and takes the duplicate's only where its own is blank, so name the richer record as KEEP. It is permanent. After `--apply` the command waits for Freshsales' background job (it re-reads a few times) and reports `verified` or `PENDING`; on PENDING tell Karthik and re-check with `contact KEEP_ID` later. Report the old ids.
- **Delete** (permanent; there is no restore and no "forget"): only when Karthik says delete, or for clearly non-software targets with no history (section 3). The dry run shows exactly what goes (record, its deals with stage, its notes) and what blocks it. The CLI enforces Karthik's guards:
  - a **contact** is refused if it is on a **live deal** (anything except Screened Out, Rejected, Sold) or has **notes** (a deal's notes count as its contacts' notes). To get past the live-deal guard, unlink first with `link-deal-contacts DEAL --remove CONTACT`, which needs Karthik's say-so; notes are history and block with no override. A contact with meetings you know of from Superhuman or Fireflies also deserves a question even though the CLI cannot see them;
  - a **deal** is refused if it has **notes**; its contacts stay and are only unlinked;
  - an **account** is refused while it has **contacts or deals**;
  - a batch is all-or-nothing, so one blocked id means nothing is deleted.
  Delete contacts with `./fs delete-contact ID [ID...]`, deals with `delete-deal`, accounts with `delete-account`. When Karthik explicitly says to remove a named contact (Peter Sokratous, Jason Harney, Joseph Bramwell, and advisors such as Steven Lindsay, Steve Crossley, Peter Anderson, Nathan Heathcliff-Core), run the dry run, apply it, and report the id and what it was linked to (it is in the output). When he gives an email list to delete, `lookup` each email first so the report names the person. When he says to delete a firm's contact and its account (Verus Corporate Ventures), delete the contact first, check the account has no other contacts or deals (`./fs account ID`), then `delete-account`. After any advisor delete, check whether an unsent Superhuman draft to that person exists and discard it (or tell him).
  Anything I would decide to remove myself is NOT deleted: list it for Karthik instead (tags are not supported by the CLI).

## 9. Report

Structured text first, per record: deal name, id and link (`https://tern.freshworks.com/crm/sales/deals/ID`), live or Screened Out and why; contacts created, linked or fixed with ids and links (`/crm/sales/contacts/ID`); prior-contact findings; merges, deletes and reopens (old ids, what moved or went); defaults used; Apollo credits used and balance; verification result (all `ok`, or the `DID NOT TAKE` rows); the SO-CF priority/status check line (0b); the city/LinkedIn/email check line (0c); failures; open flags (including unverified emails, `PENDING` merges and stale Superhuman drafts for rejected targets); and the **API calls line** from the last command (what was spent and what the account has left). For 10 or more records also deliver a CSV (reachout status, name, deal, title, area, location, email, LinkedIn, reject reason, comments, headcount, revenue, industry), validated for column count before sending.

## Fixed names and gotchas

- Owners (by name in `--set owner=`): Karthik Dasari, Simon Corson, James Hang, Joanna Te, Tom Llewellyn-Lloyd, Damian Lee, Francis Gordon. (Ids: Karthik 13000009586, Simon 113000000486, James 113000000483, Joanna 13000052361, Tom 13000052259, Damian 13000042446, Francis 13000052683.)
- The Not Qualified account is named exactly `Not Qualified` (id 13005231493). Subscription types: Deal Announcements = 2 (the others: 1 Non-marketing emails, 3 Promotional, 4 Team Updates, 5 Tern Academy).
- Stage names for `--set stage=` (pipeline All Tern Deals): `04 Plan-Teaser/Suspect`, `06 Plan-Future-No ResponseA4`, `08 Plan-NDA`, `10 Plan-Future-Second Chase`, `12 Plan-Awaiting Info`, `18 Screened Out`, `19 Live-GTM`, `20 Live-Prospect`, `24 Live-Initial Study`, `28 Live-Mgmt Meeting`, `32 Live-DD`, `36 Live-IOI`, `40 Live-LOI Stage 1`, `44 Live-LOI Stage 2`, `48 Live-Closing`, `52 Future-Followup`, `54 Future-Followup W6M`, `64 Rejected-By Tern-To Notify`, `68 Rejected-By Tern-Notified`, `72 Rejected-By Seller`, `76 Sold-To Other`, `80 Sold-To Tern`. `cf_deal_stagelatest` values: `05 Screened Out`, `10 Suspect`, `20 Prospect`, `30 Mgmt Meeting/DD`, `40 IOI - Verbal`, `41 IOI - Written`, `42 LOI Signed`, `50 Closing`, `90 Closed-Tern Rejected`, `92 Closed-Seller Rejected`, `94 Closed-Won`. If a name is wrong the CLI lists the valid ones.
- `update-contact` sends only the fields that change, and the dry run shows `same` for the rest. A read-back mismatch is a real failure to report, not noise. Dates and phone numbers are compared ignoring the formatting Freshsales applies (`2026-11-01` comes back as a timestamp, `020 0000 0000` loses its spaces).
- `--set` values containing spaces need quotes. Multi-selects use `;`. Names are case-sensitive for picklists.
- Search (lookup, find-deals, duplicate checks) lags records created in the last few minutes.
- A new contact needs a first name and an email OR a mobile number.
- LinkedIn fields take the slug only. Companies House PSC pages can be read via Exa.
- Apollo company domain can differ from the website; search by keywords if the domain returns nothing.

## Zapier action to `fs` command

| Zapier action | `fs` command |
|---|---|
| get_contacts_reachout_status, search_contact_v2 | `lookup EMAILS [--brief]` |
| search_contacts_by_name, find_contacts_for_accounts | `find-contacts --name/--email/--city/--type/--domain` |
| export_contact_rows | `contact IDS` |
| export_deal_rows, verify_deal_links | `deal IDS` (and every create/link verifies itself) |
| fuzzy_search_deals, Find Deal, Find Account | `find-deals TERMS [--details]` |
| get_account_contacts_and_deals, contacts_by_account_filter | `account ID` |
| list_view_contacts, list_contacts_by_type_and_city, find_contacts_by_job_title_keywords | `list-view VIEW --where ... --fields ...` (and `views`) |
| create_contact_action, create_contact_no_email | `create-contact --preset ...` |
| update_contact_action_new, set_contact_account_and_subscription, clear_contact_job_title | `update-contact ID --set ...` |
| set_contact_primary_email | `update-contact ID --set email=...` |
| create_deal_action, update_deal | `create-deal`, `update-deal` |
| create_account_action | `create-account --preset advisor-account` |
| update_account | `update-account ID --set name=... --set "cf_type=SO-CF;AD-A&T" ...` |
| create_note, add_note_contact | `add-note ID TEXT --on contact|deal|account` |
| Update Deal `contacts_added_list` (it REPLACED the list) | `link-deal-contacts DEAL --add/--remove` (it appends and removes) |
| merge_contacts | `merge-contacts PRIMARY --with IDS` |
| delete_contact, delete_deal, delete_sales_account | `delete-contact`, `delete-deal`, `delete-account` (guarded) |
| Create Task / Meeting / Sales Activity / custom modules | not in the CLI: ask |
