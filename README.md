# TraderEvolution Back-Office — Claude Code Plugin

A Claude Code plugin for working with **TraderEvolution Back-Office reports**. Ask in plain words
what a report shows, what one of its columns means, or which report to use — and get the answer,
or the data.

The reports live in the Back Office, which exposes them over its REST API. The API lists reports
and runs them, but never returns their definition, so this plugin keeps its own knowledge of them:
every report's columns, how the computed ones relate, and the traps that make output misleading.
Beyond reports, the whole BO Swagger specification is reachable — accounts, users, orders,
positions, plans — for the times a question needs something the reports do not carry.

The MCP server runs **locally on each user's machine** — there is nothing to host. Sharing this
plugin means sharing a repository; every colleague installs it and authenticates with their own
back-office login, so BO permissions and audit records stay per-person.

> **Just want it working?** [QUICKSTART.md](QUICKSTART.md) — five steps, in Russian.
> This README is the full reference.

> For trading operations on a client account, see the separate
> [trade-revolution](https://github.com/dmikhailenko-hash/claude-mcp-plugin) plugin. This one is
> back-office only.

## How it works

Nine tools. The three that matter most answer questions about reports and never touch the network:

| Tool | Purpose |
|---|---|
| `bo_search_reports` | Find a report by name, or by a column it contains — *which report shows `crossprice`?* Offline. |
| `bo_explain_report` | Explain one report: what a row is, every column, verified formulas, required filters, and the traps. Offline. |
| `bo_report_functions` | The report engine's column functions with their formulas, for explaining how a computed column is derived. Offline. |

Running a report, or reaching anything else in the API, goes through the rest:

| Tool | Purpose |
|---|---|
| `bo_request` | Call any endpoint, including `GET /reports/{id}` to run a report. Handles login, token refresh and the `Bearer` header. |
| `bo_search_endpoints` | Find endpoints by keyword, HTTP method or Swagger tag. |
| `bo_describe_endpoint` | Full contract for one endpoint: parameters, enums, request body schema with `$ref`s inlined, response schema. |
| `bo_status` | Show base URL, auth state, permissions and whether the spec and report catalogue are loaded. Start here when something breaks. |
| `bo_fetch_spec` | Download and cache the Swagger specification. Needed once per machine. |
| `bo_login` | Force re-authentication. Rarely needed — the other tools authenticate on demand. |

There are 634 operations across about 250 paths. Turning each into its own tool would flood the
model's context, so a question that needs one works as *search → describe → request*: Claude finds
`/accountDetails`, reads which query parameters it accepts, then calls it with the right ones.

## Reports

Brokers mostly ask *what a report means*, not for its rows. The API cannot answer that — it lists
reports and runs them, but never returns their definition. So the plugin keeps two local files:

- **`server/reports.catalog.json`** — harvested from your server by `build_report_catalog.py`, which
  runs every saved report over a one-minute window in 2019 and keeps the column headers that come
  back. Report names, ids and columns only; no client data.
- **`server/reports.notes.json`** — written by hand: what a row represents, how columns relate, and
  the traps. Deliberately a separate file, so rebuilding the catalogue never destroys it.

`bo_explain_report` merges the two, so Claude answers a question about a report without calling the
API at all. The notes are where the non-obvious things live — for instance that *Account Statement
Report* (119) **ignores `startDate` and `endDate` entirely** and returns a snapshot taken at request
time, so running it "for last month" shows today's numbers.

The catalogue is specific to the installation it was built against. Point the server at a different
BO and the tools return a `catalogWarning` telling you to rebuild.

A third file, **`server/reports.visibility.json`**, lists the clutter. Reports accumulate on a
long-lived stand: dated snapshots, `(copy)` duplicates, superseded versions, someone's test report.
Those are hidden from the report tools, and `bo_request` refuses `GET /reports/{id}` for them before
the request leaves the machine. Nothing is deleted from the back office — this is purely what the
MCP server surfaces. `TR_BO_SHOW_ALL_REPORTS=true` reveals them again for maintenance.

The list is deliberately short. **Which real reports a person may see is the Back Office's own
decision**, taken from their permissions: `GET /reports` already returns a shorter list for a
non-administrator, so there is nothing to reproduce here.

It is worth hiding clutter rather than ignoring it, because **running a report is a `GET`** —
`TR_BO_ALLOW_WRITES=false` does not stop someone executing SQL that was already saved as a report.

A bundled **skill** teaches Claude this workflow — reach for the catalogue before running a report,
surface the warnings alongside the output, decode BO error codes, and never propose disabling a
guardrail to make something work.

> The skill lives at `skills/traderevolution-bo/SKILL.md`, but Claude Code only reads skills from
> `~/.claude/skills/` and `<project>/.claude/skills/` — a skill inside a repository is picked up
> automatically only when that repository is installed through the plugin system. Where `/plugin` is
> unavailable, run `python server/install_skill.py` to copy it into place. `setup_check.py` reports
> whether it is installed.

## Requirements

- Claude Code
- **Python 3.9 or newer on `PATH`.** No packages to install — the server uses only the standard
  library.
- Network access to your BO server (VPN, if it is internal)
- Your own back-office account

## Installation

There are two routes. Try route 1 first; if your Claude Code answers
`/plugin isn't available in this environment`, use route 2. The server is identical either way —
only the wiring differs.

### Route 1 — as a plugin

```
/plugin marketplace add dmikhailenko-hash/bo-mcp-plugin
/plugin install traderevolution-bo@bo-mcp-plugin
```

Then open `/plugin`, select **traderevolution-bo**, and fill in the configuration:

| Field | Notes |
|---|---|
| **BO API base URL** | Including the basePath, no trailing slash. Default points at the demo server. |
| **BO login** / **BO password** | Your own credentials. Stored in your local Claude Code settings, never in the repository. |
| **Allow write requests** | Leave off to stay read-only. |
| **Allow money and user-destructive endpoints** | Leave off unless you are on a test server. |
| **Skip TLS verification** | Only for servers with a self-signed certificate. |

To try a local checkout instead of the GitHub copy, point the marketplace at the directory:
`/plugin marketplace add /path/to/bo-mcp-plugin`. The `claude --plugin-dir .` flag works too, but
only where the `claude` CLI is on `PATH` — the desktop app bundles it internally and does not
expose it.

### Route 2 — manually, via `.mcp.json`

This works in every environment, including those where `/plugin` is disabled.

**Step 1.** Clone the repository anywhere:

```bash
git clone https://github.com/dmikhailenko-hash/bo-mcp-plugin.git
```

**Step 2.** Register the server. For one project, add this to `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "traderevolution-bo": {
      "type": "stdio",
      "command": "python",
      "args": ["C:/path/to/bo-mcp-plugin/server/bo_mcp_server.py"]
    }
  }
}
```

To have it in every project, put the same `traderevolution-bo` block under `mcpServers` in
`~/.claude/settings.json` instead. Use forward slashes in the path even on Windows, and
`"command": "python3"` on macOS and Linux.

**Step 3.** Copy `.env.example` to `.env` **in the clone root** and fill it in:

```
TR_BO_BASE_URL=https://your-bo-server:8443/proftrading/rest
TR_BO_LOGIN=your_login
TR_BO_PASSWORD=your_password
TR_BO_ALLOW_WRITES=false
TR_BO_ALLOW_DANGEROUS=false
```

The clone root is the reliable place for it: the server looks for `.env` in `server/`, then the
clone root, then the current working directory, and stops at the first one it finds. A `.env` in the
clone root therefore works no matter which project you launch Claude Code from. `.env` is
git-ignored.

You can also pass the same values through an `env` block in `.mcp.json`, but then your password
lives in a file you might commit — `.env` is safer.

**Step 4.** Restart Claude Code. MCP configuration is read at startup.

### After either route

**Check the setup before involving Claude Code.** This script tells you exactly what is missing:

```bash
python server/setup_check.py
```

It verifies Python, finds your `.env`, opens a TCP connection to your BO host, authenticates,
downloads the Swagger spec and reports the catalogue state. Each failure says what to do about it.
Nothing is written and no password is printed.

Then restart Claude Code, confirm with `/mcp` that `traderevolution-bo` is connected, and:

> Run bo_status

If your server does not publish the spec at a guessable URL, `bo_fetch_spec` reports every URL it
tried. Save `swagger.json` yourself and point `TR_BO_SPEC_PATH` at it, or drop it in as
`server/swagger.cache.json`. `bo_request` works either way — only search and describe need the spec.

**Build the report catalogue for your own installation:**

```bash
python server/build_report_catalog.py
```

This takes a couple of minutes and probes every saved report for its columns. The repository ships a
catalogue built against the demo stand; if you point at a different BO, rebuild, or the report tools
will describe the wrong reports.

## Configuration reference

The server is configured entirely through environment variables. On route 1 the `/plugin` UI
supplies them; on route 2 they come from `.env`, or from an `env` block in `.mcp.json` if you prefer.

Anything not already set in the environment falls back to a `.env` file, searched in this order and
stopping at the first hit: `TR_BO_ENV_FILE`, `server/.env`, the clone root, then the current working
directory. A `.env` never overrides a value that is already set, so the `/plugin` UI and an `env`
block both win over it.

| Variable | Default | Meaning |
|---|---|---|
| `TR_BO_BASE_URL` | demo server | API base including basePath |
| `TR_BO_LOGIN` / `TR_BO_PASSWORD` | — | BO credentials |
| `TR_BO_ALLOW_WRITES` | `false` | `false` = GET only |
| `TR_BO_ALLOW_DANGEROUS` | `false` | money movement, deletions, password resets |
| `TR_BO_INSECURE_TLS` | `false` | skip certificate verification |
| `TR_BO_SPEC_PATH` | `server/swagger.cache.json` | local Swagger file |
| `TR_BO_CATALOG_PATH` | `server/reports.catalog.json` | local report catalogue |
| `TR_BO_SHOW_ALL_REPORTS` | `false` | `true` also surfaces reports hidden as clutter |
| `TR_BO_TIMEOUT` | `30` | per-request timeout, seconds |
| `TR_BO_PYTHON` | `python` | route 1 only: interpreter the plugin launches. Set it to `python3` on macOS and Linux. On route 2 you set `command` in `.mcp.json` directly instead. |

## Safety model

The BO API can move money, delete users and rewrite risk plans. Two independent switches stand in
the way, both off by default:

1. **Writes.** With `TR_BO_ALLOW_WRITES` off, every `POST`, `PUT`, `PATCH` and `DELETE` is refused
   before any request leaves your machine.
2. **Protected endpoints.** Even with writes on, these stay blocked until
   `TR_BO_ALLOW_DANGEROUS` is also on:
   - `/accountOperations` — deposits, withdrawals, adjustments
   - `/assetsBalances/*/deposit` and `/withdrawal`
   - `DELETE /users` and `DELETE /accounts`
   - `/resetPassword`, `/changePassword`, `/passwordBlacklist`
   - `/closeAccount*`
   - `DELETE /positions` — position rollback

Both switches are per-user, set in each person's own configuration. Nothing in this repository grants
access to anything: without credentials the server can do nothing at all.

Reports sit outside this model, because running one is a `GET` and so passes the write guard. They
are governed by the back office's own per-user permissions, plus the clutter list in
`server/reports.visibility.json`.

Beyond that, the BO API's own permission system still applies. A colleague who cannot delete users
in the back-office cannot delete them through this plugin either.

## Sharing with colleagues

Push this repository somewhere they can reach, then send them the five steps below. Each person runs
the server on their own machine against their own BO, with their own login — nothing is hosted and
the repository never carries a credential.

**What to send them:**

> **1.** Check you have Python: `python --version` (3.9 or newer; `python3` on macOS and Linux).
> If not, install it from [python.org](https://www.python.org/downloads/). There are no packages to
> install afterwards — the server uses only the standard library.
>
> **2.** Clone the repo:
> ```bash
> git clone <repo-url>
> cd bo-mcp-plugin
> ```
>
> **3.** Answer three questions — the server, your login, your password:
> ```bash
> python server/configure.py
> ```
> It offers our demo server as the default, accepts the Swagger page URL and trims it down to the
> API base, hides the password as you type, and checks the connection **before** writing anything.
> A refused login is explained in plain words instead of an error code. The answers go into `.env`,
> which is git-ignored — your password stays on your machine.
>
> Prefer to edit the file yourself? Copy `.env.example` to `.env` and set `TR_BO_BASE_URL`,
> `TR_BO_LOGIN` and `TR_BO_PASSWORD`.
>
> **4.** Verify everything, and fix whatever it reports:
> ```bash
> python server/setup_check.py
> ```
> On success it prints the exact `.mcp.json` block for your path. Paste it into `.mcp.json` in your
> project root (or into `~/.claude/settings.json` to have it everywhere) and restart Claude Code.
>
> **5.** Build the report catalogue for your stand — a couple of minutes:
> ```bash
> python server/build_report_catalog.py
> ```
>
> **6.** Install the skill, which teaches Claude how to use all of this:
> ```bash
> python server/install_skill.py
> ```
> Restart Claude Code afterwards. Re-run it whenever you pull a new version.
>
> Then ask Claude something like *"what does the Trades Report show and what do its columns mean"*.

Each person must use their **own** BO login. Do not share one account: per-user logins are what makes
the BO audit trail useful, and they keep each person inside their own BO permissions. This also means
a colleague only ever sees what the back-office already lets them see.

If you would rather run one shared server instead of one per machine, that is a different
architecture: the server has to be hosted somewhere with network access to the BO API, and it needs
its own authentication layer so it does not become an unauthenticated proxy to your back-office.
The local setup avoids both problems.

## Troubleshooting

**`traderevolution-bo` missing from `/mcp`** — Python is probably not on `PATH`. Check with
`python --version`. On macOS and Linux set `TR_BO_PYTHON=python3`. Check `/plugin` → Errors.

**"Cannot reach ..."** — wrong `TR_BO_BASE_URL`, no VPN, or a self-signed certificate. For the last
case turn on **Skip TLS verification**.

**Login fails** — verify the same credentials work in the back-office UI, and that the account has
REST API access. Error code `2.0.2.4` means the user has no API access; `1.2.0.11` means bad
login or password.

**Numeric error codes in responses** — decode them:

> Run bo_request GET /methods/errors with query errorId=2.0.2.4

**Truncated responses** — output is capped at 20 000 characters. Narrow the query with `limit`,
`offset`, `startDate` or `endDate`.

**`bo_status` says credentials are missing after you just added them** — the `.env` file is read once
at startup. Restart Claude Code.

**"No report catalogue on this machine"** — run `python server/build_report_catalog.py`.

**`catalogWarning` in a report result** — the catalogue was built against a different BO. Rebuild it.

**A report returns HTTP 406 with `1.6.2.2`** — it needs a filter you did not supply
(`PARAM / REPORT_REQUEST / FILTER_TYPE / EMPTY`). `bo_explain_report` names it where it is known;
`accountId` and `assetName` are the usual candidates.

**HTTP 429 / error `2.147.1.27`** — rate limited. The limit is a few requests per second, and
repeated violations block the token for several seconds. Space requests out instead of retrying
immediately.

## License

MIT — see [LICENSE](LICENSE).
