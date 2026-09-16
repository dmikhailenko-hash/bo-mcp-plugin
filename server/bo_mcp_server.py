#!/usr/bin/env python3
"""Back-Office MCP server for the TraderEvolution BO REST API.

Speaks MCP over stdio (newline-delimited JSON-RPC 2.0) using nothing but the
Python standard library, so it runs anywhere Python 3.9+ is installed.

Instead of exposing all 600+ Swagger operations as separate tools (which would
flood the model's context), it exposes a small set of tools that let the model
discover endpoints in the spec and then call them.

Configuration comes from environment variables, optionally seeded from a .env
file (see load_dotenv below):

    TR_BO_BASE_URL         API base, including basePath.
                           Default: https://un-demo.traderevolution.com:8443/proftrading/rest
    TR_BO_LOGIN            BO user login
    TR_BO_PASSWORD         BO user password
    TR_BO_ALLOW_WRITES     "true" to permit POST/PUT/PATCH/DELETE. Default: false
    TR_BO_ALLOW_DANGEROUS  "true" to permit money/user-destructive endpoints
                           even when writes are on. Default: false
    TR_BO_INSECURE_TLS     "true" to skip TLS verification (self-signed demo
                           certificates only). Default: false
    TR_BO_SPEC_PATH        Path to a local swagger.json. Default: cached copy
                           next to this script.
    TR_BO_TIMEOUT          Per-request timeout in seconds. Default: 30
    TR_BO_ENV_FILE         Explicit .env location.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "traderevolution-bo"
SERVER_VERSION = "1.1.0"

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SPEC_CACHE = os.path.join(SCRIPT_DIR, "swagger.cache.json")
DEFAULT_CATALOG = os.path.join(SCRIPT_DIR, "reports.catalog.json")
DEFAULT_NOTES = os.path.join(SCRIPT_DIR, "reports.notes.json")
DEFAULT_VISIBILITY = os.path.join(SCRIPT_DIR, "reports.visibility.json")

DEFAULT_BASE_URL = "https://un-demo.traderevolution.com:8443/proftrading/rest"

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
MAX_RESPONSE_CHARS = 20000

# Endpoints that move money, delete people, or reset credentials. Blocked even
# when writes are enabled, unless TR_BO_ALLOW_DANGEROUS is also set.
DANGEROUS_RULES = (
    (WRITE_METHODS, r"^/accountOperations", "account operations (deposit / withdrawal / adjustment)"),
    (WRITE_METHODS, r"^/assetsBalances/[^/]+/(deposit|withdrawal)", "asset balance deposit / withdrawal"),
    (("DELETE",), r"^/users(/|$)", "user deletion"),
    (("DELETE",), r"^/accounts(/|$)", "account deletion"),
    (WRITE_METHODS, r"^/users/[^/]+/(resetPassword|changePassword)", "password reset / change"),
    (WRITE_METHODS, r"^/closeAccount", "account closing"),
    (("DELETE",), r"^/positions(/|$)", "position rollback"),
    (WRITE_METHODS, r"^/passwordBlacklist", "password blacklist modification"),
)


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for the JSON-RPC stream."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

PLACEHOLDER_PATTERN = re.compile(r"^\$\{[^}]*\}$")

CONFIG_KEYS = (
    "TR_BO_BASE_URL",
    "TR_BO_LOGIN",
    "TR_BO_PASSWORD",
    "TR_BO_ALLOW_WRITES",
    "TR_BO_ALLOW_DANGEROUS",
    "TR_BO_INSECURE_TLS",
    "TR_BO_SPEC_PATH",
    "TR_BO_TIMEOUT",
)


def drop_unexpanded_placeholders() -> None:
    """Discard values like '${user_config.bo_login}'.

    A Claude Code version that does not support a placeholder used in .mcp.json
    passes it through literally. Treating those as unset lets the .env fallback
    take over instead of authenticating with nonsense.
    """
    for key in CONFIG_KEYS:
        value = os.environ.get(key)
        if value is not None and PLACEHOLDER_PATTERN.match(value.strip()):
            log(f"ignoring unexpanded placeholder in {key}")
            del os.environ[key]


def load_dotenv() -> None:
    """Seed os.environ from the first .env file found. Never overrides."""
    candidates = []
    explicit = os.environ.get("TR_BO_ENV_FILE")
    if explicit:
        candidates.append(explicit)
    candidates += [
        os.path.join(SCRIPT_DIR, ".env"),
        os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".env")),  # plugin root
        os.path.join(os.getcwd(), ".env"),  # last resort, for direct runs
    ]

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
            log(f"loaded environment from {path}")
        except OSError as exc:
            log(f"could not read {path}: {exc}")
        return


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    return (os.environ.get("TR_BO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def origin_url() -> str:
    parts = urllib.parse.urlsplit(base_url())
    return f"{parts.scheme}://{parts.netloc}"


def request_timeout() -> float:
    try:
        return float(os.environ.get("TR_BO_TIMEOUT") or 30)
    except ValueError:
        return 30.0


def ssl_context() -> ssl.SSLContext | None:
    if env_flag("TR_BO_INSECURE_TLS"):
        return ssl._create_unverified_context()  # noqa: SLF001 - intentional opt-in
    return None


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

class HttpResult:
    def __init__(self, status: int, headers: dict, text: str):
        self.status = status
        self.headers = headers
        self.text = text

    def json(self):
        if not self.text:
            return None
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None


def http_request(
    method: str,
    url: str,
    headers: dict | None = None,
    body: object | None = None,
) -> HttpResult:
    """Perform one HTTP call. HTTP error statuses are returned, not raised."""
    data = None
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(request, timeout=request_timeout(), context=ssl_context()) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return HttpResult(response.status, dict(response.headers), payload)
    except urllib.error.HTTPError as exc:
        payload = ""
        try:
            payload = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best effort
            pass
        return HttpResult(exc.code, dict(exc.headers or {}), payload)
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Cannot reach {url}: {exc.reason}. "
            "Check TR_BO_BASE_URL, VPN / network access to the BO server, and "
            "set TR_BO_INSECURE_TLS=true if the server uses a self-signed certificate."
        ) from exc


class ToolError(Exception):
    """Raised to return a readable failure to the model instead of crashing."""


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

class Auth:
    """Holds the access/refresh pair and keeps it fresh."""

    SKEW_SECONDS = 30

    def __init__(self) -> None:
        self.access_token: str | None = None
        self.access_expires_at: float = 0.0
        self.refresh_token: str | None = None
        self.refresh_expires_at: float = 0.0
        self.login_name: str | None = None

    # -- state -------------------------------------------------------------- #

    def access_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.access_expires_at - self.SKEW_SECONDS

    def refresh_valid(self) -> bool:
        return bool(self.refresh_token) and time.time() < self.refresh_expires_at - self.SKEW_SECONDS

    def describe(self) -> dict:
        if self.access_valid():
            state = "authenticated"
        elif self.refresh_valid():
            state = "access token expired, refresh token still valid"
        else:
            state = "not authenticated"
        return {
            "state": state,
            "login": self.login_name,
            "access_token_expires_in_seconds": max(0, int(self.access_expires_at - time.time()))
            if self.access_token
            else None,
        }

    # -- transitions -------------------------------------------------------- #

    def _store(self, payload: dict) -> None:
        access = payload.get("accessToken")
        refresh = payload.get("refreshToken")
        if not access:
            raise ToolError(f"Auth response contained no accessToken: {json.dumps(payload)[:500]}")

        now = time.time()
        self.access_token = access
        self.access_expires_at = now + float(payload.get("accessTokenLifeTime") or 300)
        if refresh:
            self.refresh_token = refresh
            self.refresh_expires_at = now + float(payload.get("refreshTokenLifeTime") or 3600)

    def login(self, login: str | None = None, password: str | None = None) -> dict:
        login = login or os.environ.get("TR_BO_LOGIN")
        password = password or os.environ.get("TR_BO_PASSWORD")

        if not login or not password:
            raise ToolError(
                "No BO credentials available. Set them up by running, in the plugin directory:\n"
                "    python server/configure.py\n"
                "It asks for the server, your login and your password, checks they work, and "
                "writes .env. Then restart Claude Code, because .env is read once at startup."
            )

        result = http_request(
            "POST",
            f"{base_url()}/auth/token",
            body={"login": login, "password": password},
        )

        if result.status != 200:
            raise ToolError(
                f"Login failed with HTTP {result.status}: {result.text[:800] or '(empty body)'}"
            )

        payload = result.json()
        if not isinstance(payload, dict):
            raise ToolError(f"Login returned a non-JSON body: {result.text[:500]}")

        self._store(payload)
        self.login_name = login
        log(f"authenticated as {login}")
        return self.describe()

    def refresh(self) -> bool:
        if not self.refresh_valid():
            return False

        result = http_request(
            "GET",
            f"{base_url()}/auth/token/refresh",
            headers={"token": self.refresh_token or ""},
        )

        if result.status != 200:
            log(f"token refresh failed with HTTP {result.status}")
            return False

        payload = result.json()
        if not isinstance(payload, dict):
            return False

        self._store(payload)
        log("access token refreshed")
        return True

    def ensure(self) -> None:
        if self.access_valid():
            return
        if self.refresh() and self.access_valid():
            return
        self.login()

    def header(self) -> dict:
        self.ensure()
        return {"Authorization": f"Bearer {self.access_token}"}

    def invalidate_access(self) -> None:
        self.access_token = None
        self.access_expires_at = 0.0


AUTH = Auth()


# --------------------------------------------------------------------------- #
# Swagger spec: loading, search, description
# --------------------------------------------------------------------------- #

class Spec:
    def __init__(self) -> None:
        self.document: dict | None = None
        self.source: str | None = None
        self.operations: list[dict] = []

    # -- loading ------------------------------------------------------------ #

    def load_from_disk(self) -> bool:
        for path in (os.environ.get("TR_BO_SPEC_PATH"), DEFAULT_SPEC_CACHE):
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    document = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                log(f"could not parse spec at {path}: {exc}")
                continue
            self._index(document, path)
            return True
        return False

    def fetch_from_server(self) -> str:
        candidates = [
            f"{base_url()}/swagger.json",
            f"{base_url()}/v2/api-docs",
            f"{base_url()}/api-docs",
            f"{origin_url()}/v2/api-docs",
            f"{origin_url()}/swagger.json",
        ]

        attempts = []
        for url in candidates:
            for authenticated in (False, True):
                headers = {}
                if authenticated:
                    try:
                        headers = AUTH.header()
                    except ToolError:
                        continue
                try:
                    result = http_request("GET", url, headers=headers)
                except ToolError as exc:
                    attempts.append(f"{url} -> {exc}")
                    continue

                if result.status == 200:
                    document = result.json()
                    if isinstance(document, dict) and document.get("paths"):
                        self._index(document, url)
                        self._save_cache()
                        return url
                    attempts.append(f"{url} -> HTTP 200 but no 'paths' in body")
                else:
                    attempts.append(f"{url} -> HTTP {result.status}")

        detail = "\n".join(f"  - {line}" for line in attempts)
        raise ToolError(
            "Could not download the Swagger specification automatically. Tried:\n"
            f"{detail}\n\n"
            "Save the spec manually as swagger.json and point TR_BO_SPEC_PATH at it, or copy it to "
            f"{DEFAULT_SPEC_CACHE}. bo_request works without the spec — only the discovery tools "
            "(bo_search_endpoints, bo_describe_endpoint) need it."
        )

    def _save_cache(self) -> None:
        if not self.document:
            return
        try:
            with open(DEFAULT_SPEC_CACHE, "w", encoding="utf-8") as handle:
                json.dump(self.document, handle)
            log(f"cached spec at {DEFAULT_SPEC_CACHE}")
        except OSError as exc:
            log(f"could not write spec cache: {exc}")

    def _index(self, document: dict, source: str) -> None:
        self.document = document
        self.source = source
        self.operations = []

        for path, methods in (document.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                if not isinstance(operation, dict):
                    continue
                self.operations.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "summary": operation.get("summary") or "",
                        "operationId": operation.get("operationId") or "",
                        "tags": operation.get("tags") or [],
                        "deprecated": bool(operation.get("deprecated")),
                        "raw": operation,
                    }
                )

        self.operations.sort(key=lambda item: (item["path"], item["method"]))
        log(f"indexed {len(self.operations)} operations from {source}")

    def ensure_loaded(self) -> None:
        if self.operations:
            return
        if self.load_from_disk():
            return
        self.fetch_from_server()

    # -- queries ------------------------------------------------------------ #

    def search(self, query: str, method: str | None, tag: str | None, limit: int) -> list[dict]:
        self.ensure_loaded()
        terms = [term for term in re.split(r"[\s,/_-]+", (query or "").lower()) if term]

        scored = []
        for operation in self.operations:
            if method and operation["method"] != method.upper():
                continue
            if tag and not any(tag.lower() in existing.lower() for existing in operation["tags"]):
                continue

            haystack = " ".join(
                [
                    operation["path"],
                    operation["summary"],
                    operation["operationId"],
                    " ".join(operation["tags"]),
                ]
            ).lower()

            if not terms:
                score = 1
            else:
                score = 0
                for term in terms:
                    if term in operation["path"].lower():
                        score += 3
                    elif term in haystack:
                        score += 1
                if score == 0:
                    continue

            if operation["deprecated"]:
                score -= 1

            scored.append((score, operation))

        scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))

        return [
            {
                "method": operation["method"],
                "path": operation["path"],
                "summary": operation["summary"],
                "tags": operation["tags"],
                "deprecated": operation["deprecated"],
            }
            for _, operation in scored[:limit]
        ]

    def describe(self, path: str, method: str) -> dict:
        self.ensure_loaded()
        wanted_method = method.upper()

        match = next(
            (
                operation
                for operation in self.operations
                if operation["path"] == path and operation["method"] == wanted_method
            ),
            None,
        )

        if match is None:
            near = [
                f"{operation['method']} {operation['path']}"
                for operation in self.operations
                if operation["path"] == path
            ]
            hint = f" Available methods for this path: {', '.join(near)}." if near else ""
            raise ToolError(f"No operation {wanted_method} {path} in the specification.{hint}")

        operation = match["raw"]
        parameters = []
        body_schema = None

        for parameter in operation.get("parameters") or []:
            if not isinstance(parameter, dict):
                continue
            if parameter.get("in") == "body":
                body_schema = self._resolve(parameter.get("schema"))
                continue
            parameters.append(
                {
                    "name": parameter.get("name"),
                    "in": parameter.get("in"),
                    "required": bool(parameter.get("required")),
                    "type": parameter.get("type"),
                    "enum": parameter.get("enum"),
                    "default": parameter.get("default"),
                    "description": (parameter.get("description") or "").strip()[:400] or None,
                }
            )

        responses = {}
        for status, response in (operation.get("responses") or {}).items():
            if status.startswith("2") and isinstance(response, dict):
                responses[status] = {
                    "description": response.get("description"),
                    "schema": self._resolve(response.get("schema"), max_depth=2),
                }

        return {
            "method": wanted_method,
            "path": path,
            "summary": operation.get("summary"),
            "tags": operation.get("tags"),
            "deprecated": bool(operation.get("deprecated")),
            "parameters": parameters,
            "requestBodySchema": body_schema,
            "successResponses": responses,
        }

    def _resolve(self, schema, max_depth: int = 3, depth: int = 0, seen: tuple = ()):
        """Inline $ref definitions, depth-limited and cycle-safe."""
        if not isinstance(schema, dict) or not self.document:
            return schema

        ref = schema.get("$ref")
        if ref:
            name = ref.split("/")[-1]
            if name in seen or depth >= max_depth:
                return {"$ref": name, "note": "not expanded (depth limit or recursion)"}
            definition = (self.document.get("definitions") or {}).get(name)
            if definition is None:
                return {"$ref": name, "note": "definition not found"}
            resolved = self._resolve(definition, max_depth, depth + 1, seen + (name,))
            if isinstance(resolved, dict):
                resolved = dict(resolved)
                resolved["title"] = resolved.get("title") or name
            return resolved

        output = {}
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                output[key] = {
                    prop: self._resolve(sub, max_depth, depth + 1, seen)
                    for prop, sub in value.items()
                }
            elif key == "items":
                output[key] = self._resolve(value, max_depth, depth + 1, seen)
            elif key == "description" and isinstance(value, str):
                output[key] = value.strip()[:300]
            else:
                output[key] = value
        return output


SPEC = Spec()


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #

class Catalog:
    """Knowledge about the saved reports, as opposed to the REST surface.

    The API lists reports and runs them but never returns their definition, so
    two files stand in for it: reports.catalog.json, harvested from the server
    by build_report_catalog.py, and reports.notes.json, written by hand. They
    are kept apart so that rebuilding the first never destroys the second.
    """

    def __init__(self) -> None:
        self.catalog: dict | None = None
        self.notes: dict = {}
        self.visibility: dict = {}
        self.last_search_hidden = 0
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True

        catalog_path = os.environ.get("TR_BO_CATALOG_PATH") or DEFAULT_CATALOG
        try:
            with open(catalog_path, "r", encoding="utf-8-sig") as handle:
                self.catalog = json.load(handle)
            log(f"loaded report catalogue from {catalog_path}")
        except (OSError, json.JSONDecodeError) as exc:
            log(f"no report catalogue ({exc})")

        try:
            with open(DEFAULT_NOTES, "r", encoding="utf-8-sig") as handle:
                notes = json.load(handle)
            self.notes = {key: value for key, value in notes.items()
                          if not key.startswith("_")}
        except (OSError, json.JSONDecodeError):
            self.notes = {}

        try:
            with open(DEFAULT_VISIBILITY, "r", encoding="utf-8-sig") as handle:
                document = json.load(handle)
            self.visibility = document.get("hidden") or {}
        except (OSError, json.JSONDecodeError):
            self.visibility = {}

    # -- visibility --------------------------------------------------------- #
    #
    # The Back Office already restricts reports per user, so this layer only
    # removes stand noise: copies, dated snapshots, superseded versions, tests.

    def show_all(self) -> bool:
        return env_flag("TR_BO_SHOW_ALL_REPORTS")

    def hidden_reason(self, report_id) -> str | None:
        return self.visibility.get(str(report_id))

    def visible(self, report_id) -> bool:
        if self.show_all():
            return True
        return self.hidden_reason(report_id) is None

    def require_visible(self, report_id, name: str = "") -> None:
        if self.visible(report_id):
            return
        label = f"{name} ({report_id})" if name else str(report_id)
        raise ToolError(
            f"Report {label} is hidden: {self.hidden_reason(report_id)}\n"
            "It is listed in server/reports.visibility.json as leftover clutter on this "
            "installation. Set TR_BO_SHOW_ALL_REPORTS=true to reach it anyway, or remove its "
            "entry from that file if it is in fact needed."
        )

    def stale_for_base_url(self) -> str | None:
        """Catalogues are per-installation: report ids differ between stands.

        Whoever points this server at their own BO must rebuild, or they will
        be reading explanations of somebody else's reports.
        """
        self.load()
        if not self.catalog:
            return None
        built_against = (self.catalog.get("baseUrl") or "").rstrip("/")
        current = base_url().rstrip("/")
        if built_against and built_against != current:
            return (
                f"This report catalogue was built against {built_against}, but the connection "
                f"points at {current}. Report ids and names differ between installations, so "
                "these explanations may describe different reports. Rebuild with: "
                "python server/build_report_catalog.py"
            )
        return None

    def require(self) -> dict:
        self.load()
        if not self.catalog:
            raise ToolError(
                "No report catalogue on this machine. Build it once with:\n"
                "    python server/build_report_catalog.py\n"
                "It takes about two minutes and probes every saved report for its columns. "
                "Until then, list reports with bo_request GET /reports and run one with "
                "bo_request GET /reports/{id}."
            )
        return self.catalog

    def reports(self) -> list[dict]:
        return self.require().get("reports") or []

    def functions(self) -> list[dict]:
        return self.require().get("functions") or []

    def note_for(self, report_id) -> dict:
        self.load()
        return self.notes.get(str(report_id)) or {}

    def search(self, query: str, column: str | None, limit: int) -> list[dict]:
        terms = [term for term in re.split(r"[\s,/_-]+", (query or "").lower()) if term]
        needle = (column or "").lower().strip()

        scored = []
        hidden = 0
        for report in self.reports():
            if not self.visible(report.get("id")):
                hidden += 1
                continue
            name = (report.get("name") or "").lower()
            columns = [str(item).lower() for item in (report.get("columns") or [])]

            if needle and not any(needle in item for item in columns):
                continue

            if not terms:
                score = 1
            else:
                score = 0
                for term in terms:
                    if term in name:
                        score += 3
                    elif any(term in item for item in columns):
                        score += 1
                if score == 0:
                    continue

            entry = {
                "id": report.get("id"),
                "name": report.get("name"),
                "columnCount": len(report.get("columns") or []),
            }
            reason = self.hidden_reason(report.get("id"))
            if reason:
                entry["hidden"] = reason

            note = self.note_for(report.get("id"))
            if note.get("summary"):
                entry["summary"] = note["summary"]
            if note.get("warnings"):
                entry["warningCount"] = len(note["warnings"])
            if not report.get("ok"):
                entry["probeFailed"] = report.get("error")
            if needle:
                entry["matchedColumns"] = [
                    item for item in (report.get("columns") or [])
                    if needle in str(item).lower()
                ]
            scored.append((score, entry))

        scored.sort(key=lambda item: (-item[0], str(item[1].get("name"))))
        self.last_search_hidden = hidden
        return [entry for _, entry in scored[:limit]]

    def explain(self, report_id) -> dict:
        wanted = str(report_id)
        found = None
        for report in self.reports():
            if str(report.get("id")) == wanted:
                found = report
                break
        if found is None:
            raise ToolError(
                f"Report {report_id!r} is not in the catalogue. Search for it with "
                "bo_search_reports, or rebuild the catalogue if the report is new: "
                "python server/build_report_catalog.py"
            )

        self.require_visible(found.get("id"), found.get("name") or "")

        payload: dict = {
            "id": found.get("id"),
            "name": found.get("name"),
            "runWith": f"bo_request GET /reports/{found.get('id')}",
            "columns": found.get("columns") or [],
        }
        reason = self.hidden_reason(found.get("id"))
        if reason:
            payload["hidden"] = reason
        if not found.get("ok"):
            payload["probeFailed"] = found.get("error")
        if found.get("requiresExtraFilter"):
            payload["requiresExtraFilter"] = True
            payload["probedWith"] = found.get("probedWith")

        # Merge the hand-written notes, but never let them clobber the harvested
        # facts: a note's "columns" explains some columns, it is not the list.
        note = dict(self.note_for(found.get("id")))
        column_notes = note.pop("columns", None)
        note.pop("id", None)
        note.pop("name", None)
        payload.update(note)
        if column_notes:
            payload["columnNotes"] = column_notes

        # The column names are SQL aliases; the function glossary explains the
        # ones that are computed rather than stored.
        glossary = {}
        for function in self.functions():
            key = str(function.get("name") or function.get("id") or "").lower()
            if not key:
                continue
            for column in payload["columns"]:
                if key and key in str(column).lower():
                    glossary[str(column)] = function
                    break
        if glossary:
            payload["columnFunctions"] = glossary

        payload["filterParameters"] = (
            "Every report accepts the same query parameters. Read them with "
            "bo_describe_endpoint path=/reports/{reportId} method=GET."
        )
        return payload


CATALOG = Catalog()


def check_permitted(method: str, path: str) -> None:
    method = method.upper()

    if method in WRITE_METHODS and not env_flag("TR_BO_ALLOW_WRITES"):
        raise ToolError(
            f"{method} is blocked: this server is running read-only. "
            "Only GET requests are allowed. To permit writing, set TR_BO_ALLOW_WRITES=true "
            "in your .env and restart Claude Code."
        )

    if env_flag("TR_BO_ALLOW_DANGEROUS"):
        return

    for methods, pattern, label in DANGEROUS_RULES:
        if method in methods and re.match(pattern, path, re.IGNORECASE):
            raise ToolError(
                f"{method} {path} is blocked because it touches {label}. "
                "These endpoints are protected separately from ordinary writes. "
                "To permit them, set TR_BO_ALLOW_DANGEROUS=true in your .env and restart "
                "Claude Code — do this only on a demo or test server."
            )


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def tool_status(_: dict) -> dict:
    spec_state: dict = {"loaded": bool(SPEC.operations)}
    if SPEC.operations:
        spec_state.update({"operations": len(SPEC.operations), "source": SPEC.source})
    else:
        spec_state["hint"] = "run bo_fetch_spec, or set TR_BO_SPEC_PATH to a local swagger.json"

    credentials_configured = bool(
        os.environ.get("TR_BO_LOGIN") and os.environ.get("TR_BO_PASSWORD")
    )

    auth_state = AUTH.describe()
    if credentials_configured and not AUTH.access_token:
        auth_state["note"] = (
            "Credentials are present but unused so far. This server logs in lazily, on the first "
            "request, so 'not authenticated' here is expected right after startup and does not "
            "indicate a problem."
        )
    elif not credentials_configured:
        auth_state["note"] = (
            "No credentials found. Tell the user to run 'python server/configure.py' in the "
            "plugin directory: it asks for the server, login and password, verifies them and "
            "writes .env. Never ask them for the password yourself. Claude Code must be "
            "restarted afterwards, because .env is read once at startup."
        )

    CATALOG.load()
    catalog_state: dict = {"loaded": bool(CATALOG.catalog), "annotatedReports": len(CATALOG.notes)}
    if CATALOG.catalog:
        reports = CATALOG.reports()
        catalog_state["reports"] = len(reports)
        catalog_state["probedSuccessfully"] = sum(1 for item in reports if item.get("ok"))
        catalog_state["columnFunctions"] = len(CATALOG.functions())
        catalog_state["generatedAt"] = CATALOG.catalog.get("generatedAt")
        catalog_state["builtAgainst"] = CATALOG.catalog.get("baseUrl")
        visible = sum(1 for item in reports if CATALOG.visible(item.get("id")))
        catalog_state["reportsVisible"] = visible
        catalog_state["reportsHidden"] = len(reports) - visible
        if CATALOG.show_all():
            catalog_state["showAllReports"] = True
        stale = CATALOG.stale_for_base_url()
        if stale:
            catalog_state["warning"] = stale
    else:
        catalog_state["hint"] = (
            "Build it once with: python server/build_report_catalog.py — takes about two minutes. "
            "bo_search_reports and bo_explain_report need it."
        )

    return {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "baseUrl": base_url(),
        "credentialsConfigured": credentials_configured,
        "auth": auth_state,
        "reportCatalog": catalog_state,
        "permissions": {
            "writesAllowed": env_flag("TR_BO_ALLOW_WRITES"),
            "dangerousEndpointsAllowed": env_flag("TR_BO_ALLOW_DANGEROUS"),
            "tlsVerification": "disabled" if env_flag("TR_BO_INSECURE_TLS") else "enabled",
        },
        "spec": spec_state,
    }


def tool_login(arguments: dict) -> dict:
    return AUTH.login(arguments.get("login"), arguments.get("password"))


def tool_fetch_spec(_: dict) -> dict:
    source = SPEC.fetch_from_server()
    return {
        "source": source,
        "operations": len(SPEC.operations),
        "cachedAt": DEFAULT_SPEC_CACHE,
    }


def tool_search_endpoints(arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 25)
    limit = max(1, min(limit, 100))
    results = SPEC.search(
        query=arguments.get("query") or "",
        method=arguments.get("method"),
        tag=arguments.get("tag"),
        limit=limit,
    )
    return {"count": len(results), "results": results}


def tool_describe_endpoint(arguments: dict) -> dict:
    path = arguments.get("path")
    method = arguments.get("method")
    if not path or not method:
        raise ToolError("bo_describe_endpoint requires both 'path' and 'method'.")
    return SPEC.describe(path, method)


def check_report_permitted(path: str) -> None:
    """Block running a report that is hidden as stand clutter.

    Running a report is a GET, so the read-only guard does not cover it at all:
    reports are arbitrary SQL that somebody already saved.
    """
    match = re.match(r"^/reports/(\d+)\b", path)
    if not match:
        return
    CATALOG.load()
    if not CATALOG.visibility:
        return
    report_id = match.group(1)
    name = ""
    for report in (CATALOG.catalog or {}).get("reports") or []:
        if str(report.get("id")) == report_id:
            name = report.get("name") or ""
            break
    CATALOG.require_visible(report_id, name)


def tool_search_reports(arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 25)
    limit = max(1, min(limit, 100))
    results = CATALOG.search(
        query=arguments.get("query") or "",
        column=arguments.get("column"),
        limit=limit,
    )
    # The warning goes first: a caller that truncates, or summarises only the
    # head of the payload, must still see that these reports are not theirs.
    payload: dict = {}
    stale = CATALOG.stale_for_base_url()
    if stale:
        payload["catalogWarning"] = stale
    payload["count"] = len(results)
    if CATALOG.last_search_hidden:
        payload["hiddenFromThisConnection"] = CATALOG.last_search_hidden
    payload["results"] = results
    return payload


def tool_explain_report(arguments: dict) -> dict:
    report_id = arguments.get("reportId")
    if report_id is None or report_id == "":
        raise ToolError("bo_explain_report requires 'reportId'.")
    explained = CATALOG.explain(report_id)
    stale = CATALOG.stale_for_base_url()
    if not stale:
        return explained
    # Same reasoning as in bo_search_reports: warning first, so it survives.
    return {"catalogWarning": stale, **explained}


def tool_report_functions(arguments: dict) -> dict:
    query = (arguments.get("query") or "").lower().strip()
    functions = CATALOG.functions()
    if query:
        functions = [
            function for function in functions
            if query in json.dumps(function, ensure_ascii=False).lower()
        ]
    return {"count": len(functions), "functions": functions}


def tool_request(arguments: dict) -> dict:
    method = (arguments.get("method") or "GET").upper()
    path = arguments.get("path") or ""
    query = arguments.get("query") or {}
    body = arguments.get("body")

    if not path.startswith("/"):
        raise ToolError(
            f"'path' must start with '/' and exclude the base URL. Got: {path!r}. "
            "Example: /accounts or /users/42"
        )
    if method not in ("GET",) + WRITE_METHODS:
        raise ToolError(f"Unsupported method {method!r}.")
    if not isinstance(query, dict):
        raise ToolError("'query' must be an object mapping parameter names to values.")

    check_permitted(method, path)
    check_report_permitted(path)

    url = base_url() + path
    if query:
        flat = {
            key: ("true" if value is True else "false" if value is False else value)
            for key, value in query.items()
            if value is not None
        }
        if flat:
            url += "?" + urllib.parse.urlencode(flat, doseq=True)

    result = http_request(method, url, headers=AUTH.header(), body=body)

    # A stale access token can survive our own expiry bookkeeping; retry once.
    if result.status == 401:
        log("got HTTP 401, re-authenticating and retrying once")
        AUTH.invalidate_access()
        result = http_request(method, url, headers=AUTH.header(), body=body)

    text = result.text
    truncated = False
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS]
        truncated = True

    payload: dict = {"request": {"method": method, "url": url}, "status": result.status}

    parsed = None
    if not truncated:
        try:
            parsed = json.loads(result.text) if result.text else None
        except json.JSONDecodeError:
            parsed = None

    if parsed is not None:
        payload["body"] = parsed
    elif text:
        payload["bodyText"] = text
    else:
        payload["body"] = None

    if truncated:
        payload["truncated"] = (
            f"Response exceeded {MAX_RESPONSE_CHARS} characters and was cut. "
            "Narrow the result with query parameters such as limit, offset, startDate or endDate."
        )

    if result.status >= 400:
        payload["hint"] = (
            "The BO API returns error codes like 2.0.2.4 or 1.2.0.11. "
            "Decode them with bo_request GET /methods/errors?errorId=<code>."
        )

    return payload


TOOLS = [
    {
        "name": "bo_status",
        "description": (
            "Report how this Back-Office connection is configured: base URL, whether credentials "
            "are present, authentication state, whether writes and money-touching endpoints are "
            "permitted, and whether the Swagger spec is loaded. Call this first when something "
            "does not work."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_status,
    },
    {
        "name": "bo_login",
        "description": (
            "Authenticate against the Back-Office API (POST /auth/token) and cache the access and "
            "refresh tokens in memory. Credentials default to TR_BO_LOGIN / TR_BO_PASSWORD from the "
            "environment, so this is usually unnecessary: other tools authenticate and refresh "
            "automatically. Tokens are never returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "login": {"type": "string", "description": "BO user login. Defaults to TR_BO_LOGIN."},
                "password": {
                    "type": "string",
                    "description": "BO user password. Defaults to TR_BO_PASSWORD.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_login,
    },
    {
        "name": "bo_search_endpoints",
        "description": (
            "Search the Back-Office Swagger specification for endpoints by keyword, HTTP method or "
            "tag. Use this to find the right endpoint before calling bo_request — the API has "
            "over 600 operations. Returns method, path, summary and tags."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords, e.g. 'account operations', 'orders', 'risk plan'.",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "Restrict to one HTTP method.",
                },
                "tag": {
                    "type": "string",
                    "description": "Restrict to a Swagger tag, e.g. 'Accounts', 'Orders', 'Users'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results, 1-100. Default 25.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_search_endpoints,
    },
    {
        "name": "bo_describe_endpoint",
        "description": (
            "Return the full contract for one Back-Office endpoint: path, query and header "
            "parameters with their types, enums and defaults, the request body schema with $ref "
            "definitions inlined, and the success response schema. Call this before any non-trivial "
            "bo_request so the parameters are correct."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Exact Swagger path, e.g. /accounts/{accountId} or /orders.",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "HTTP method of the operation.",
                },
            },
            "required": ["path", "method"],
            "additionalProperties": False,
        },
        "handler": tool_describe_endpoint,
    },
    {
        "name": "bo_request",
        "description": (
            "Call any Back-Office REST endpoint. Handles authentication, token refresh and the "
            "Bearer header. Path is relative to the API base and may contain no host, e.g. "
            "'/accountDetails'. Writing methods are refused unless the connection is configured to "
            "allow them, and endpoints that move money or delete users stay blocked separately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "HTTP method. Default GET.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the API base, starting with '/', with path parameters "
                        "already substituted. Example: /accounts/128 not /accounts/{accountId}."
                    ),
                },
                "query": {
                    "type": "object",
                    "description": "Query parameters as an object. Nulls are dropped.",
                    "additionalProperties": True,
                },
                "body": {
                    "description": "JSON request body for POST, PUT and PATCH.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": tool_request,
    },
    {
        "name": "bo_search_reports",
        "description": (
            "Find a saved Back-Office report by name or by a column it contains. Use this before "
            "bo_explain_report when the report id is unknown. Reads a local catalogue built from "
            "the server, so it costs no API call. Searching by column answers questions like "
            "'which report shows crossprice'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords from the report name, e.g. 'trades', 'statement', 'margin'.",
                },
                "column": {
                    "type": "string",
                    "description": "Only return reports having a column whose name contains this.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results, 1-100. Default 25.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_search_reports,
    },
    {
        "name": "bo_explain_report",
        "description": (
            "Explain one saved report: what a single row represents, every column it returns, how "
            "the computed columns relate to each other, which filters it needs, and the traps that "
            "make its output misleading. Answer a broker's question about a report with this before "
            "running the report itself. Costs no API call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reportId": {
                    "description": "Numeric report id, e.g. 117 for Trades Report.",
                    "type": ["integer", "string"],
                },
            },
            "required": ["reportId"],
            "additionalProperties": False,
        },
        "handler": tool_explain_report,
    },
    {
        "name": "bo_report_functions",
        "description": (
            "List the column functions the Back-Office report engine offers, with their formulas. "
            "Use this to explain how a computed report column is derived. Optionally filter by "
            "keyword."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to filter the glossary, e.g. 'margin', 'profit', 'cross'.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_report_functions,
    },
    {
        "name": "bo_fetch_spec",
        "description": (
            "Download the Back-Office Swagger specification from the server and cache it on disk so "
            "that bo_search_endpoints and bo_describe_endpoint work. Only needed once per machine, "
            "or after the API is upgraded."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_fetch_spec,
    },
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------- #
# MCP protocol
# --------------------------------------------------------------------------- #

def public_tools() -> list[dict]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
        }
        for tool in TOOLS
    ]


def call_tool(name: str, arguments: dict) -> dict:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }

    try:
        result = tool["handler"](arguments or {})
        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:  # noqa: BLE001 - never take the server down
        log(f"unhandled error in {name}: {exc!r}")
        return {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }


def handle_message(message: dict) -> dict | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    # Notifications carry no id and get no response.
    if message_id is None:
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def fail(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return ok(
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "TraderEvolution Back-Office REST API. Find endpoints with bo_search_endpoints, "
                    "read their contract with bo_describe_endpoint, then call them with bo_request. "
                    "Use bo_status to inspect configuration and permissions."
                ),
            }
        )

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": public_tools()})

    if method == "tools/call":
        return ok(call_tool(params.get("name") or "", params.get("arguments") or {}))

    # Declared capabilities cover tools only; answer the rest harmlessly.
    if method == "resources/list":
        return ok({"resources": []})
    if method == "prompts/list":
        return ok({"prompts": []})

    return fail(-32601, f"Method not found: {method}")


def configure_streams() -> None:
    """Force UTF-8 on the JSON-RPC streams.

    On Windows the default stdout encoding is the ANSI code page, which turns
    any non-ASCII character into bytes an MCP client cannot decode. newline
    is pinned so the protocol stream stays LF-delimited.
    """
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover - Python < 3.7
        pass


def main() -> None:
    configure_streams()
    drop_unexpanded_placeholders()
    load_dotenv()
    log(
        f"starting, base={base_url()}, "
        f"writes={'on' if env_flag('TR_BO_ALLOW_WRITES') else 'off'}, "
        f"dangerous={'on' if env_flag('TR_BO_ALLOW_DANGEROUS') else 'off'}"
    )

    # Preload the spec if one is already on disk; absence is not an error.
    try:
        SPEC.load_from_disk()
    except Exception as exc:  # noqa: BLE001
        log(f"spec preload skipped: {exc!r}")

    stdout = sys.stdout
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"ignoring malformed JSON line: {exc}")
            continue

        if isinstance(message, list):
            responses = [handle_message(item) for item in message if isinstance(item, dict)]
            batch = [item for item in responses if item is not None]
            if batch:
                stdout.write(json.dumps(batch) + "\n")
                stdout.flush()
            continue

        if not isinstance(message, dict):
            continue

        try:
            response = handle_message(message)
        except Exception as exc:  # noqa: BLE001
            log(f"unhandled error handling {message.get('method')!r}: {exc!r}")
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()

    log("stdin closed, exiting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
