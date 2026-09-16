#!/usr/bin/env python3
"""Ask for the Back-Office server and credentials, verify them, and write .env.

    python server/configure.py

Nothing is sent anywhere except a login request to the server you name. The
password is typed without echo, is never printed, and goes straight into your
own .env file, which is git-ignored.

    --env PATH   write somewhere other than the plugin root
    --show       print the resulting file without the password (for support)
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
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
DEFAULT_ENV = os.path.join(PLUGIN_ROOT, ".env")
DEMO_URL = "https://un-demo.traderevolution.com:8443/proftrading/rest"

MANAGED_KEYS = ("TR_BO_BASE_URL", "TR_BO_LOGIN", "TR_BO_PASSWORD", "TR_BO_INSECURE_TLS")

# Things people paste instead of the base URL, because that is what their
# browser shows them.
SWAGGER_SUFFIXES = (
    "/swagger-ui.html",
    "/swagger-ui/index.html",
    "/swagger-ui",
    "/swagger.json",
    "/v2/api-docs",
    "/api-docs",
)


def normalise_base_url(raw: str) -> str:
    """Turn whatever the user pasted into a base URL."""
    value = raw.strip().strip('"').strip("'")
    if not value:
        return value
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value

    parsed = urllib.parse.urlparse(value)
    path = parsed.path
    # Drop the query and fragment a browser adds, then any Swagger UI suffix.
    for suffix in SWAGGER_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))


class NotInteractive(Exception):
    """Raised when there is nobody at the keyboard to answer."""


def read_line(prompt: str) -> str:
    """input(), but a closed or redirected stdin is reported, not a traceback.

    isatty() is not reliable enough on its own - under some Windows shells it
    reports a terminal and then the first read hits EOF.
    """
    try:
        return input(prompt)
    except EOFError as exc:
        raise NotInteractive from exc
    except KeyboardInterrupt:
        print("\nCancelled. Nothing written.")
        sys.exit(1)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = read_line(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("  This one is required.")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [y/N]" if not default else " [Y/n]"
    answer = read_line(f"{prompt}{suffix}: ").strip().lower()
    if not answer:
        return default
    return answer.startswith(("y", "д"))


def ask_password(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError as exc:
        raise NotInteractive from exc
    except KeyboardInterrupt:
        print("\nCancelled. Nothing written.")
        sys.exit(1)


def read_existing(path: str) -> list[str]:
    """Keep the user's own comments and any settings this script does not manage."""
    for source in (path, os.path.join(PLUGIN_ROOT, ".env.example")):
        if os.path.isfile(source):
            with open(source, "r", encoding="utf-8-sig") as handle:
                return handle.read().splitlines()
    return []


def apply_values(lines: list[str], values: dict[str, str]) -> list[str]:
    remaining = dict(values)
    result = []
    for line in lines:
        match = re.match(r"^(\s*)(" + "|".join(MANAGED_KEYS) + r")\s*=", line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            result.append(f"{key}={remaining.pop(key)}")
        else:
            result.append(line)

    if remaining:
        if result and result[-1].strip():
            result.append("")
        for key, value in remaining.items():
            result.append(f"{key}={value}")
    return result


# The codes a first-time setup actually runs into, in plain words.
ERROR_HINTS = {
    "1.2.0.11": "The login or the password is wrong.",
    "2.0.2.4": (
        "This account exists but has no REST API access. Ask a back-office "
        "administrator to enable it."
    ),
    "2.147.1.27": "Too many requests - the server is rate limiting. Wait a few seconds.",
}


def explain_failure(message: str) -> str:
    for code, hint in ERROR_HINTS.items():
        if code in message:
            return hint
    # Order matters, and the match has to be narrow: the server's own error text
    # ends with advice mentioning a self-signed certificate, so a loose search
    # for "certificate" labels every failure a TLS problem.
    if "CERTIFICATE_VERIFY_FAILED" in message or "SSLCertVerificationError" in message:
        return "The server's TLS certificate was rejected. It may be self-signed."
    if "getaddrinfo failed" in message or "Name or service not known" in message:
        return "That host name does not resolve. Check the address for a typo."
    if "Cannot reach" in message:
        return (
            "The server did not answer. Check the address and the port, and whether "
            "this machine needs a VPN to reach it."
        )
    return ""


def verify(base: str, login: str, password: str, insecure: bool) -> bool:
    os.environ["TR_BO_BASE_URL"] = base
    os.environ["TR_BO_INSECURE_TLS"] = "true" if insecure else "false"
    bo.AUTH.invalidate_access()
    try:
        info = bo.AUTH.login(login, password)
    except bo.ToolError as exc:
        message = str(exc)
        hint = explain_failure(message)
        print(f"\n  Failed. {hint}" if hint else "\n  Failed.")
        print(f"  Details: {message[:400]}\n")
        return False
    print(f"\n  Connected. Authenticated as {info.get('login')}.\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=DEFAULT_ENV)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    if args.show:
        if not os.path.isfile(args.env):
            print(f"No file at {args.env}")
            return 1
        with open(args.env, "r", encoding="utf-8-sig") as handle:
            for line in handle.read().splitlines():
                if re.match(r"^\s*TR_BO_PASSWORD\s*=", line):
                    print("TR_BO_PASSWORD=<hidden>")
                else:
                    print(line)
        return 0

    # Two guards, because neither is sufficient on its own. isatty() catches a
    # redirected stdin, which matters most on Windows: getpass there reads the
    # console directly and would block for ever on a pipe instead of failing.
    # NotInteractive catches the opposite case, a stdin that claims to be a
    # terminal and is immediately at EOF.
    try:
        if not sys.stdin.isatty():
            raise NotInteractive
        return interactive(args)
    except NotInteractive:
        print(
            "\nThis script asks questions, so it needs a terminal with somebody at it.\n"
            "Run it directly in your shell - not through a pipe, a task runner or an agent.\n"
            "Alternatively, copy .env.example to .env and fill it in by hand.",
            file=sys.stderr,
        )
        return 1


def interactive(args) -> int:
    print("TraderEvolution Back-Office - configuration\n")
    print("Three answers are needed: the server, your login, and your password.\n")

    print("The server address is the API base: scheme, host, port and base path.")
    print("It is NOT the address of the Swagger page - if your Swagger UI is at")
    print("    https://my-bo.example.com:8443/proftrading/rest/swagger-ui.html")
    print("then the answer is")
    print("    https://my-bo.example.com:8443/proftrading/rest")
    print("Paste either one; the Swagger suffix is stripped automatically.")
    print(f"Press Enter to use our demo server: {DEMO_URL}\n")

    base = normalise_base_url(ask("BO server", DEMO_URL))
    print(f"  Using: {base}\n")

    print("Use your own back-office account, not a shared one: every request is")
    print("made as you, so BO permissions and audit records stay per-person.\n")

    insecure = False
    while True:
        login = ask("BO login")
        password = ask_password("BO password (not shown as you type): ")
        if not password:
            print("  A password is required.\n")
            continue

        print("\nChecking...")
        if verify(base, login, password, insecure):
            break

        if not insecure and ask_yes_no(
            "Does this server use a self-signed certificate? Retry without TLS verification",
            default=False,
        ):
            insecure = True
            print("\nChecking again without TLS verification...")
            if verify(base, login, password, insecure):
                break

        if not ask_yes_no("Try different credentials", default=True):
            if ask_yes_no("Save anyway, without a working connection", default=False):
                break
            print("Nothing written.")
            return 1

    values = {
        "TR_BO_BASE_URL": base,
        "TR_BO_LOGIN": login,
        "TR_BO_PASSWORD": password,
        "TR_BO_INSECURE_TLS": "true" if insecure else "false",
    }

    if os.path.isfile(args.env):
        print(f"{args.env} already exists. Its other settings and comments are kept;")
        if not ask_yes_no("only the four values above are replaced. Continue", default=True):
            print("Nothing written.")
            return 1

    lines = apply_values(read_existing(args.env), values)
    with open(args.env, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")

    # Best effort: on POSIX this makes the file owner-only. Windows ignores it,
    # where NTFS permissions on a user profile directory already apply.
    try:
        os.chmod(args.env, 0o600)
    except OSError:
        pass

    print(f"\nWritten to {args.env}")
    print("This file is git-ignored, so your password stays on this machine.\n")
    print("Next:")
    print("    python server/setup_check.py            confirm everything")
    print("    python server/build_report_catalog.py   learn this stand's reports")
    print("    python server/install_skill.py          teach Claude the workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
