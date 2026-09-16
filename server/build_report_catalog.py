#!/usr/bin/env python3
"""Build the report catalogue used by bo_search_reports and bo_explain_report.

The Back-Office REST API lists reports and runs them, but never returns their
definition, so a report's columns can only be learned by executing it. This
script runs every report over a one-minute window far in the past, which
normally yields no rows, and keeps the column headers that come back anyway.

Run it once per machine:

    python server/build_report_catalog.py

Options:
    --limit N     only process the first N reports (for a quick trial)
    --timeout S   per-report timeout in seconds (default 25)
    --out PATH    where to write the catalogue
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# A colleague's console may be on cp866, cp1251 or cp437, none of which can
# represent every character. Degrade to '?' rather than raising.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):  # pragma: no cover - Python < 3.7, or no console
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bo_mcp_server as bo  # noqa: E402

# A one-minute window in 2019: old enough that most reports return no rows,
# which keeps this cheap, while still returning the column headers.
PROBE_START = 1546300800000
PROBE_END = 1546300860000

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports.catalog.json")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch(path: str, query: dict | None = None):
    url = bo.base_url() + path
    if query:
        import urllib.parse

        url += "?" + urllib.parse.urlencode(query)
    result = bo.http_request("GET", url, headers=bo.AUTH.header())
    return result.status, result.json(), result.text


def fetch_with_retry(path: str, query: dict | None, attempts: int, backoff: float):
    """Retry on HTTP 429.

    The API rate-limits requests and, after blockingThreshold violations,
    blocks the token for blockingDuration seconds, so a plain retry is not
    enough - we have to wait out the block.
    """
    for attempt in range(1, attempts + 1):
        status, body, text = fetch(path, query)
        if status != 429:
            return status, body, text, attempt
        if attempt < attempts:
            wait = backoff * attempt
            log(f"        rate limited, waiting {wait:.0f}s (attempt {attempt}/{attempts})")
            time.sleep(wait)
    return status, body, text, attempts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--delay", type=float, default=0.6,
                        help="pause between reports, seconds (rate limiting)")
    parser.add_argument("--attempts", type=int, default=4,
                        help="attempts per report when rate limited")
    parser.add_argument("--backoff", type=float, default=12.0,
                        help="base wait after HTTP 429, seconds")
    parser.add_argument("--slow-timeout", type=float, default=120.0,
                        help="timeout for the retry of a report that timed out")
    args = parser.parse_args()

    bo.drop_unexpanded_placeholders()
    bo.load_dotenv()
    os.environ["TR_BO_TIMEOUT"] = str(args.timeout)

    log(f"base: {bo.base_url()}")
    bo.AUTH.login()

    # These preliminary calls need the same 429 handling as the reports below.
    # Without it a rate-limited glossary silently produces an empty one, and the
    # catalogue looks complete when it is not.
    setup = lambda path, query=None: fetch_with_retry(  # noqa: E731
        path, query, args.attempts, args.backoff)

    status, reports, _, _ = setup("/reports")
    if status != 200 or not isinstance(reports, list):
        log(f"cannot list reports: HTTP {status}")
        return 1
    log(f"reports: {len(reports)}")

    time.sleep(args.delay)
    status, functions, text, _ = setup("/reports/functions")
    if status != 200 or not isinstance(functions, list):
        log(f"WARNING: no column glossary, HTTP {status}: {(text or '')[:120]}")
        log("         bo_report_functions will be empty. Re-run to fill it in.")
        functions = []
    log(f"column functions: {len(functions)}")

    # Some reports refuse to run without a filter, and which filter they want
    # is not discoverable - the API only says "a filter is missing". Build a
    # chain of candidates and try them in turn.
    fallbacks = []

    time.sleep(args.delay)
    status, accounts, _, _ = setup("/accounts", {"limit": 1})
    if status == 200 and isinstance(accounts, list) and accounts:
        account_id = accounts[0].get("id")
        fallbacks.append(("accountId", {"accountId": account_id}))
        log(f"fallback probe account: {account_id}")

    time.sleep(args.delay)
    status, assets, _, _ = setup("/assets", {"limit": 1})
    asset_name = None
    if status == 200 and isinstance(assets, list) and assets:
        asset_name = assets[0].get("name")
    asset_name = asset_name or "USD"
    fallbacks.append(("assetName", {"assetName": asset_name}))
    log(f"fallback probe asset: {asset_name}")

    if len(fallbacks) == 2:
        fallbacks.append(("accountId+assetName",
                          {**fallbacks[0][1], **fallbacks[1][1]}))

    if args.limit:
        reports = reports[: args.limit]

    entries = []
    started = time.time()

    for index, report in enumerate(reports, 1):
        report_id = report.get("id")
        name = report.get("name")
        entry = {"id": report_id, "name": name}
        began = time.time()

        probe = {"startDate": PROBE_START, "endDate": PROBE_END, "limit": 1}

        try:
            status, body, text, attempts_used = fetch_with_retry(
                f"/reports/{report_id}", probe, args.attempts, args.backoff
            )

            # 1.6.2.2 is PARAM / REPORT_REQUEST / FILTER_TYPE / EMPTY: the report
            # demands a filter we did not supply. Work through the candidates.
            if status == 406 and "1.6.2.2" in (text or ""):
                entry["requiresExtraFilter"] = True
                for label, extra in fallbacks:
                    time.sleep(args.delay)
                    status, body, text, attempts_used = fetch_with_retry(
                        f"/reports/{report_id}",
                        dict(probe, **extra),
                        args.attempts,
                        args.backoff,
                    )
                    if status == 200:
                        entry["probedWith"] = label
                        break

            if status == 200 and isinstance(body, dict):
                entry["columns"] = body.get("headers") or []
                entry["probeRows"] = len(body.get("data") or [])
                entry["ok"] = True
            else:
                entry["ok"] = False
                entry["error"] = f"HTTP {status}: {(text or '')[:200]}"
            if attempts_used > 1:
                entry["attempts"] = attempts_used
        except Exception as exc:  # noqa: BLE001 - one bad report must not stop the run
            # A few reports are genuinely slow. Give those a second, longer
            # chance rather than raising the timeout for all 96.
            entry["slow"] = True
            log(f"        slow report, retrying with {args.slow_timeout:.0f}s timeout")
            os.environ["TR_BO_TIMEOUT"] = str(args.slow_timeout)
            try:
                status, body, text, attempts_used = fetch_with_retry(
                    f"/reports/{report_id}", probe, args.attempts, args.backoff
                )
                if status == 200 and isinstance(body, dict):
                    entry["columns"] = body.get("headers") or []
                    entry["probeRows"] = len(body.get("data") or [])
                    entry["ok"] = True
                else:
                    entry["ok"] = False
                    entry["error"] = f"HTTP {status}: {(text or '')[:200]}"
            except Exception as retry_exc:  # noqa: BLE001
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc} (retry: {retry_exc})"
            finally:
                os.environ["TR_BO_TIMEOUT"] = str(args.timeout)

        entry["probeSeconds"] = round(time.time() - began, 1)
        entries.append(entry)

        mark = "ok" if entry.get("ok") else "FAIL"
        columns = len(entry.get("columns") or [])
        log(f"  [{index}/{len(reports)}] {mark:4} {entry['probeSeconds']:>5.1f}s "
            f"{columns:>3} cols  {name}")

        if index < len(reports):
            time.sleep(args.delay)

    catalogue = {
        "generatedAt": int(time.time() * 1000),
        "baseUrl": bo.base_url(),
        "functions": functions,
        "reports": entries,
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(catalogue, handle, indent=2, ensure_ascii=False)

    ok = sum(1 for e in entries if e.get("ok"))
    log(f"\ndone in {time.time() - started:.0f}s: {ok}/{len(entries)} reports probed")
    log(f"written to {args.out} ({os.path.getsize(args.out):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
