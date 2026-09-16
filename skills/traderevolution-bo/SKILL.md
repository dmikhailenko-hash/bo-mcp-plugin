---
name: traderevolution-bo
description: Work with a TraderEvolution Back-Office installation over its REST API — explain a saved report and what its columns mean, run reports, find and call any of the ~630 BO endpoints, and set up or diagnose the connection. Use whenever someone asks about a BO report, a BO endpoint, or why the BO MCP connection is not working.
---

# TraderEvolution Back-Office

The `traderevolution-bo` MCP server exposes one Back-Office installation. Every
user connects with their own BO login, so BO permissions and audit records stay
per-person.

## Before anything else

Run `bo_status`. It reports the base URL, whether credentials are present,
whether the Swagger spec and the report catalogue are loaded, and whether writes
are permitted. Most problems are visible there.

Two things about it that look like faults and are not:

- `auth.authenticated: false` right after startup is normal. The server logs in
  lazily, on the first real request.
- Credentials are read from `.env` **once, at startup**. If someone just edited
  the file, Claude Code has to be restarted before the change takes effect.

If `bo_status` shows no credentials, or a request fails to connect, tell the user
to run `python server/setup_check.py`. It checks Python, the `.env` file, the
skill, TCP reachability, authentication, the spec and the catalogue, and prints
what to fix.

If they have no configuration at all, point them at `python server/configure.py`,
which asks for the server, login and password, verifies them and writes `.env`.

**Never ask the user for their password, and never write credentials into a file
for them.** Those scripts prompt for it themselves, without echo, and it goes
straight into their own local `.env`. Restarting Claude Code is required
afterwards, because `.env` is read once at startup.

## Answering a question about a report

This is the common case: a broker asks what a report is, or what one of its
columns means. **Do not run the report to answer that.** Use the catalogue —
it costs no API call and it carries analysis the API does not return.

1. `bo_search_reports` with keywords, or with `column` to find which report
   contains a given column.
2. `bo_explain_report` with the id. It returns what one row represents, the
   column list, verified formulas between columns, required filters, and the
   traps that make the output misleading.
3. Only then, if the user actually wants data, run it:
   `bo_request GET /reports/{id}` with `startDate` and `endDate` in epoch
   **milliseconds**.

`bo_report_functions` lists the report engine's column functions with their
formulas — use it to explain a computed column.

Always surface a report's `warnings` when you present its output. They exist
because the output is misleading without them. For example, `Account Statement
Report` (119) ignores `startDate` and `endDate` entirely and returns a snapshot
taken at request time: running it "for last month" shows today's numbers.

### If the catalogue is missing or stale

`bo_search_reports` and `bo_explain_report` need `server/reports.catalog.json`.
If it is absent, tell the user to run `python server/build_report_catalog.py`
(about two minutes — it probes every saved report for its columns).

A `catalogWarning` in a result means the catalogue was built against a different
BO installation — most often the one shipped in the repository, which belongs to
the demo stand, on a connection now pointing somewhere else.

**Do not present those reports as the person's own.** Report ids and names differ
between installations, so a name that looks right may be a different report or
may not exist for them at all. Lead with the warning, and tell them to run
`python server/build_report_catalog.py`, which takes a couple of minutes and
replaces the catalogue with theirs. Answer from it only after that.

## Calling any other endpoint

The API has roughly 630 operations, far too many to enumerate. Work in three
steps and do not skip the middle one:

1. `bo_search_endpoints` — find the endpoint by keyword, method or tag.
2. `bo_describe_endpoint` — read its real parameters, enums and body schema.
3. `bo_request` — call it.

If the spec is not loaded, `bo_fetch_spec` downloads and caches it.

## Safety

The connection is **read-only by default**: `TR_BO_ALLOW_WRITES=false` means GET
only. Separately, `TR_BO_ALLOW_DANGEROUS=false` blocks endpoints that move money
or destroy people — account operations, user and account deletion, password
resets, position rollbacks — even when writes are on.

Never suggest turning either on to "make something work". Enabling them is the
user's decision, it belongs in their `.env`, and it should happen only against a
demo or test server. If a request is refused by a guard, say what was blocked and
why, and stop there.

### Reports

Which reports a person may see is decided by the Back Office, from their own
permissions: `GET /reports` already returns a shorter list for a non-administrator.
Do not try to reproduce that logic.

What the BO cannot know is which reports are clutter — dated snapshots, `(copy)`
duplicates, superseded versions, someone's test. Those are listed in
`server/reports.visibility.json` and hidden from the report tools;
`bo_request` also blocks `GET /reports/{id}` for them. This matters because
**running a saved report is a `GET`**, so read-only mode does not cover it —
reports are arbitrary SQL.

If a report is refused as hidden, say which one and quote the reason. It can be
un-hidden by removing its entry from that file, which is a deliberate decision
for the user to make.

## Interpreting errors

BO returns dotted error codes such as `2.147.1.27` or `1.6.2.2`. Decode them:

    bo_request GET /methods/errors?errorId=<code>

Two you will meet often:

- `2.147.1.27` — rate limited. The limit is a few requests per second per
  collection, and repeated violations block the token for several seconds. Space
  requests out rather than retrying immediately.
- `1.6.2.2` — `PARAM / REPORT_REQUEST / FILTER_TYPE / EMPTY`. The report needs a
  filter that was not supplied. `bo_explain_report` names it where it is known;
  `accountId` and `assetName` are the usual candidates.

## Response size

Responses are truncated past 20000 characters. When that happens, narrow the
request — a shorter date range, a `limit`, or a filter such as `login` or
`accountId` — rather than re-running the same call.
