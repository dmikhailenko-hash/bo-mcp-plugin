#!/usr/bin/env python3
"""Check that this machine can talk to a Back-Office server, and say what is missing.

Run this first, before wiring the MCP server into Claude Code:

    python server/setup_check.py

Every check either passes or explains what to do about it. Nothing is written
and no credentials are printed - the password is read from your .env and only
ever sent to the BO server you configured.
"""

from __future__ import annotations

import os
import socket
import sys
import urllib.parse

# A colleague's console may be on cp866, cp1251 or cp437, none of which can
# represent every character. Degrade to '?' rather than raising.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):  # pragma: no cover - Python < 3.7, or no console
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bo_mcp_server as bo  # noqa: E402

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = "  OK  "
FAIL = " FAIL "
WARN = " WARN "

failures = 0


def report(ok: bool, title: str, detail: str = "", fatal: bool = True) -> bool:
    global failures
    mark = PASS if ok else (FAIL if fatal else WARN)
    print(f"[{mark}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    if not ok and fatal:
        failures += 1
    return ok


def check_python() -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info >= (3, 8)
    report(
        ok,
        f"Python {version}",
        "" if ok else "Python 3.8 or newer is required. Install it and re-run.",
    )


def check_env_file() -> None:
    candidates = [
        os.environ.get("TR_BO_ENV_FILE"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(PLUGIN_ROOT, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    found = [path for path in candidates if path and os.path.isfile(path)]
    if found:
        report(True, f"Configuration file: {found[0]}")
    else:
        report(
            False,
            "No .env file found",
            "Nothing is configured yet. Answer three questions:\n"
            "    python server/configure.py\n"
            "Or copy .env.example to .env in the plugin root and fill in\n"
            "TR_BO_BASE_URL, TR_BO_LOGIN and TR_BO_PASSWORD by hand.\n"
            "Looked in:\n  " + "\n  ".join(path for path in candidates if path),
        )


def check_credentials() -> bool:
    base = bo.base_url()
    report(True, f"Base URL: {base}")

    login = os.environ.get("TR_BO_LOGIN")
    password = os.environ.get("TR_BO_PASSWORD")
    ok = bool(login and password)
    report(
        ok,
        f"Credentials present (login: {login or 'MISSING'})",
        "" if ok else "Set TR_BO_LOGIN and TR_BO_PASSWORD in your .env.",
    )
    return ok


def check_reachable() -> bool:
    parsed = urllib.parse.urlparse(bo.base_url())
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return report(False, "Base URL has no host", f"TR_BO_BASE_URL={bo.base_url()!r}")

    try:
        with socket.create_connection((host, port), timeout=10):
            pass
    except OSError as exc:
        return report(
            False,
            f"Cannot open a connection to {host}:{port}",
            f"{exc}\n"
            "The host may need a VPN, or the BO service may be down. A host that\n"
            "answers ping but refuses this port means the service, not the network.",
        )
    return report(True, f"TCP connection to {host}:{port}")


def check_auth() -> bool:
    try:
        info = bo.AUTH.login()
    except bo.ToolError as exc:
        return report(
            False,
            "Authentication failed",
            f"{str(exc)[:400]}\n"
            "Check TR_BO_LOGIN and TR_BO_PASSWORD, and that the account is not locked.\n"
            "For a self-signed certificate set TR_BO_INSECURE_TLS=true.",
        )
    return report(True, f"Authenticated as {info.get('login')}")


def check_spec() -> None:
    try:
        source = bo.SPEC.fetch_from_server()
    except bo.ToolError as exc:
        report(
            False,
            "Could not download the Swagger specification",
            f"{str(exc)[:400]}",
            fatal=False,
        )
        return
    report(True, f"Swagger specification: {len(bo.SPEC.operations)} operations from {source}")


def check_catalog() -> None:
    bo.CATALOG.load()
    if not bo.CATALOG.catalog:
        report(
            False,
            "No report catalogue",
            "bo_search_reports and bo_explain_report need it. Build it with:\n"
            "    python server/build_report_catalog.py\n"
            "It takes a couple of minutes.",
            fatal=False,
        )
        return

    reports = bo.CATALOG.reports()
    probed = sum(1 for item in reports if item.get("ok"))
    stale = bo.CATALOG.stale_for_base_url()
    report(
        not stale,
        f"Report catalogue: {probed}/{len(reports)} reports, "
        f"{len(bo.CATALOG.functions())} column functions",
        stale or "",
        fatal=False,
    )

    visible = sum(1 for item in reports if bo.CATALOG.visible(item.get("id")))
    hidden = len(reports) - visible
    report(
        True,
        f"Reports surfaced: {visible} of {len(reports)}"
        + (f", {hidden} hidden as clutter" if hidden else ""),
        "Listed in server/reports.visibility.json. TR_BO_SHOW_ALL_REPORTS=true reveals them."
        if hidden else "",
    )


def check_skill() -> None:
    """The bundled skill only loads from ~/.claude/skills or <project>/.claude/skills."""
    name = "traderevolution-bo"
    candidates = [
        os.path.join(os.path.expanduser("~"), ".claude", "skills", name),
        os.path.join(os.getcwd(), ".claude", "skills", name),
    ]
    installed = [path for path in candidates if os.path.isfile(os.path.join(path, "SKILL.md"))]
    if installed:
        report(True, f"Skill installed: {installed[0]}")
        return
    report(
        False,
        "Skill not installed",
        "The MCP tools work without it, but Claude will not know the report\n"
        "workflow or the safety rules. Install it with:\n"
        "    python server/install_skill.py\n"
        "then restart Claude Code.",
        fatal=False,
    )


def main() -> int:
    print("TraderEvolution Back-Office MCP - setup check\n")

    bo.drop_unexpanded_placeholders()
    bo.load_dotenv()

    check_python()
    check_env_file()
    check_skill()

    if check_credentials() and check_reachable() and check_auth():
        check_spec()
        check_catalog()

    print()
    if failures:
        print(f"{failures} check(s) failed. Fix the items marked FAIL, then run this again.")
        return 1

    # Forward slashes: a Windows path with backslashes is not valid JSON.
    server_path = os.path.join(PLUGIN_ROOT, "server", "bo_mcp_server.py").replace("\\", "/")
    print("Ready. Add the server to your .mcp.json and restart Claude Code:")
    print(
        '  "traderevolution-bo": {\n'
        '    "type": "stdio",\n'
        '    "command": "python",\n'
        f'    "args": ["{server_path}"]\n'
        "  }"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
