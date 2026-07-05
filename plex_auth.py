import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass

import config

_PLEX_AUTH_BASE_URL = "https://plex.tv"
_PLEX_APP_AUTH_URL = "https://app.plex.tv/auth#"
_POLL_INTERVAL_SECONDS = 2.0
_PIN_TIMEOUT_FALLBACK_SECONDS = 900


@dataclass(frozen=True)
class PlexPinSession:
    pin_id: int
    code: str
    auth_url: str
    expires_in: int
    client_identifier: str


@dataclass(frozen=True)
class PlexTokenResult:
    auth_token: str
    client_identifier: str


def _ssl_context() -> ssl.SSLContext | None:
    """Always verify TLS for plex.tv.

    PLEX_VERIFY_SSL exists to tolerate the *local* Plex server's self-signed
    certificate (see plex_api.py). It must NOT weaken the connection to the
    public plex.tv auth endpoint, which has a valid certificate — an
    unverified context there would expose the account token to MITM.
    Returning None makes urllib use the default verifying context.
    """
    return None


def _auth_headers(client_identifier: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": config.APP_PRODUCT_NAME,
        "X-Plex-Version": config.APP_VERSION,
        "X-Plex-Platform": "Windows",
    }


def _request_json(
    path: str,
    *,
    client_identifier: str,
    method: str = "GET",
    query: dict[str, str] | None = None,
) -> dict[str, object]:
    url = f"{_PLEX_AUTH_BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    request = urllib.request.Request(
        url,
        headers=_auth_headers(client_identifier),
        method=method,
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=config.PLEX_REQUEST_TIMEOUT_SECONDS,
            context=_ssl_context(),
        ) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Plex auth request failed with HTTP {exc.code} for {path}: {details or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Plex auth connection failed for {path}: {exc.reason}") from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Plex auth returned invalid JSON for {path}.") from exc

    if isinstance(parsed, dict):
        return parsed
    raise RuntimeError(f"Plex auth returned an unexpected payload for {path}.")


def _json_int(value: object | None, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _read_env_lines() -> list[str]:
    if not config.DOTENV_PATH.exists():
        return []
    return config.DOTENV_PATH.read_text(encoding="utf-8").splitlines()


def _write_env_value(key: str, value: str) -> None:
    lines = _read_env_lines()
    prefix = f"{key}="
    replaced = False
    updated_lines: list[str] = []

    for line in lines:
        if line.startswith(prefix):
            updated_lines.append(f"{key}={value}")
            replaced = True
        else:
            updated_lines.append(line)

    if not replaced:
        updated_lines.append(f"{key}={value}")

    content = "\n".join(updated_lines).rstrip() + "\n"
    config.DOTENV_PATH.write_text(content, encoding="utf-8")


def _set_runtime_value(key: str, value: str) -> None:
    os.environ[key] = value
    setattr(config, key, value)


def get_or_create_client_identifier() -> str:
    client_identifier = config.PLEX_CLIENT_IDENTIFIER.strip()
    if client_identifier:
        return client_identifier

    client_identifier = uuid.uuid4().hex
    _write_env_value("PLEX_CLIENT_IDENTIFIER", client_identifier)
    _set_runtime_value("PLEX_CLIENT_IDENTIFIER", client_identifier)
    return client_identifier


def _build_auth_url(client_identifier: str, code: str) -> str:
    query = urllib.parse.urlencode(
        {
            "clientID": client_identifier,
            "code": code,
            "context[device][product]": config.APP_PRODUCT_NAME,
            "context[device][platform]": "Windows",
            "context[device][version]": config.APP_VERSION,
        }
    )
    return f"{_PLEX_APP_AUTH_URL}?{query}"


def start_plex_pin_login() -> PlexPinSession:
    client_identifier = get_or_create_client_identifier()
    payload = _request_json(
        "/api/v2/pins",
        client_identifier=client_identifier,
        method="POST",
        query={"strong": "true"},
    )
    pin_id = _json_int(payload.get("id"))
    code = str(payload.get("code") or "").strip()
    expires_in = _json_int(
        payload.get("expiresIn"),
        default=_PIN_TIMEOUT_FALLBACK_SECONDS,
    )
    if not pin_id or not code:
        raise RuntimeError("Plex did not return a usable PIN session.")

    return PlexPinSession(
        pin_id=pin_id,
        code=code,
        auth_url=_build_auth_url(client_identifier, code),
        expires_in=max(expires_in, 60),
        client_identifier=client_identifier,
    )


def launch_auth_browser(session: PlexPinSession) -> bool:
    return bool(webbrowser.open(session.auth_url, new=2))


def wait_for_plex_token(session: PlexPinSession) -> PlexTokenResult:
    deadline = time.monotonic() + session.expires_in
    while time.monotonic() < deadline:
        payload = _request_json(
            f"/api/v2/pins/{session.pin_id}",
            client_identifier=session.client_identifier,
        )
        auth_token = str(payload.get("authToken") or "").strip()
        if auth_token:
            return PlexTokenResult(
                auth_token=auth_token,
                client_identifier=session.client_identifier,
            )
        time.sleep(_POLL_INTERVAL_SECONDS)

    raise TimeoutError("Timed out waiting for Plex authorization.")


def save_plex_credentials(result: PlexTokenResult) -> None:
    _write_env_value("PLEX_CLIENT_IDENTIFIER", result.client_identifier)
    _write_env_value("PLEX_TOKEN", result.auth_token)
    _set_runtime_value("PLEX_CLIENT_IDENTIFIER", result.client_identifier)
    _set_runtime_value("PLEX_TOKEN", result.auth_token)
