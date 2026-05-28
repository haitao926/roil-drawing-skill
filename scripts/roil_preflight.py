#!/usr/bin/env python3
"""Report Roil Drawing runtime availability without exposing secrets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
from urllib import error, request


DEFAULT_PLATFORM_URL = "https://image.roil.top/"
DEFAULT_LAN_BASE_URL = "http://10.15.46.72:8002"
DEFAULT_AUTH_PATH = Path.home() / ".nbs" / "auth.json"
PENDING_SYNC_FILENAME = "cli_sync_pending.json"
KEY_NAMES = ("ROIL_API_KEY", "OPENAI_API_KEY", "IMAGE_API_KEY")
DEFAULT_PROBE_TIMEOUT = 8.0
PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def normalize_base_url(value: str | None) -> str:
    text = str(value or "").strip()
    return text.rstrip("/")


def auth_file_path() -> Path:
    override = os.environ.get("NBS_AUTH_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_AUTH_PATH


def pending_sync_path() -> Path:
    return auth_file_path().parent / PENDING_SYNC_FILENAME


def load_auth_session() -> tuple[dict, Path]:
    path = auth_file_path()
    if not path.exists():
        return {}, path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, path
    return payload if isinstance(payload, dict) else {}, path


def save_auth_session(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_pending_cli_sync() -> None:
    pending_sync_path().unlink(missing_ok=True)


def save_pending_cli_sync(sync_info: dict) -> None:
    device_code = str(sync_info.get("device_code") or "").strip()
    if not device_code:
        return
    expires_in = max(1, int(sync_info.get("expires_in") or 600))
    payload = {
        "base_url": normalize_base_url(sync_info.get("base_url")),
        "device_code": device_code,
        "verification_uri": sync_info.get("verification_uri"),
        "verification_uri_complete": sync_info.get("verification_uri_complete"),
        "user_code": sync_info.get("user_code"),
        "expires_in": expires_in,
        "interval": max(1, int(sync_info.get("interval") or 2)),
        "expires_at": time.time() + expires_in,
    }
    path = pending_sync_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pending_cli_sync(platform_url: str) -> dict | None:
    path = pending_sync_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        clear_pending_cli_sync()
        return None
    if not isinstance(payload, dict) or not str(payload.get("device_code") or "").strip():
        clear_pending_cli_sync()
        return None
    if float(payload.get("expires_at") or 0) <= time.time():
        clear_pending_cli_sync()
        return None

    base_url = normalize_base_url(payload.get("base_url") or platform_url)
    expected_base_url = normalize_base_url(platform_url)
    if base_url and expected_base_url and base_url != expected_base_url:
        clear_pending_cli_sync()
        return None
    remaining = max(1, int(float(payload.get("expires_at") or time.time()) - time.time()))
    return {
        "attempted": False,
        "available": True,
        "success": True,
        "base_url": base_url,
        "status_code": None,
        "device_code": payload.get("device_code"),
        "verification_uri": payload.get("verification_uri"),
        "verification_uri_complete": payload.get("verification_uri_complete"),
        "user_code": payload.get("user_code"),
        "expires_in": remaining,
        "interval": max(1, int(payload.get("interval") or 2)),
        "pending_reused": True,
        "pending_path": str(path),
        "error": None,
    }


def detect_nbs_cli() -> dict:
    candidates = []
    explicit = os.environ.get("NBS_CLI_PATH", "").strip()
    if explicit:
        candidates.append((Path(explicit).expanduser(), "env"))
    candidates.append((Path.cwd() / "nbs", "cwd"))

    # Roil Drawing is often invoked from a sibling project such as xedu-client,
    # while the executable NBS CLI lives in ../nano-banana-studio/nbs.
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidates.append((parent / "nano-banana-studio" / "nbs", "sibling_repo"))

    for candidate, source in candidates:
        local = candidate.resolve()
        if local.exists() and local.is_file():
            return {"available": True, "path": str(local), "source": source}

    found = shutil.which("nbs")
    if found:
        return {"available": True, "path": found, "source": "path"}
    return {"available": False, "path": None, "source": None}


def build_decision_summary(recommended_next_step: str) -> dict:
    if recommended_next_step == "generate_via_nbs_cli_backend":
        return {
            "category": "cli_backend_ready",
            "reason": "Authenticated NBS session and local CLI are both available.",
        }
    if recommended_next_step == "generate_via_platform_backend":
        return {
            "category": "platform_backend_ready",
            "reason": "Authenticated Roil session is available; local NBS CLI is not required.",
        }
    if recommended_next_step == "sync_cli_from_browser":
        return {
            "category": "cli_browser_sync_available",
            "reason": "The local Roil session is missing or stale, but the Roil platform can mint a fresh local session from the logged-in browser.",
        }
    if recommended_next_step == "try_nbs_cli_direct":
        return {
            "category": "cli_direct_only",
            "reason": "Local CLI is available but no authenticated backend session was confirmed.",
        }
    if recommended_next_step == "open_or_login_platform":
        return {
            "category": "platform_login_handoff",
            "reason": "Roil platform login is the most reliable next step for this runtime.",
        }
    return {
        "category": "manual_platform_check",
        "reason": "Neither a local CLI path nor a reachable platform probe was confirmed.",
    }


def build_recommended_commands(platform_url: str = DEFAULT_PLATFORM_URL, nbs_cli: dict | None = None) -> dict:
    script_dir = Path(__file__).resolve().parent
    preflight_script = script_dir / "roil_preflight.py"
    draw_script = script_dir / "roil_draw.py"
    sync_base_url = normalize_base_url(platform_url) or normalize_base_url(DEFAULT_PLATFORM_URL)
    if nbs_cli and nbs_cli.get("available"):
        sync_web = f"{nbs_cli.get('path') or 'nbs'} auth sync-web --base-url {sync_base_url}"
    else:
        sync_web = f'python3 {draw_script} --prompt "..." --open-platform --json'
    return {
        "draw": (
            f'python3 {draw_script} --prompt "..." '
            '--out output/roil-drawing/roil-drawing.png --open-platform --json'
        ),
        "preflight": f"python3 {preflight_script} --json",
        "sync_web": sync_web,
    }


def _probe_session_via_cli(cli_path: str, auth_info: dict, timeout: float) -> dict:
    result = {
        "attempted": False,
        "success": False,
        "base_url": None,
        "username": None,
        "quota_remaining": None,
        "quota_limit": None,
        "quota_used": None,
        "error": None,
    }
    auth_path = str(auth_info.get("auth_file_path") or "").strip()
    if not cli_path or not auth_info.get("auth_file_present") or not auth_path:
        return result

    candidate_base_urls = auth_info.get("candidate_base_urls") or []
    env = os.environ.copy()
    env["NBS_AUTH_FILE"] = auth_path

    for base_url in candidate_base_urls:
        cmd = [cli_path, "auth", "whoami", "--base-url", base_url, "--json"]
        result["attempted"] = True
        result["base_url"] = base_url
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(10, int(timeout * 2)),
                check=False,
                env=env,
            )
        except Exception as exc:
            result["error"] = str(exc)
            continue

        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except Exception:
            payload = {}

        if completed.returncode == 0 and isinstance(payload, dict) and payload.get("success") and payload.get("username"):
            result["success"] = True
            result["username"] = payload.get("username")
            result["quota_remaining"] = payload.get("quota_remaining")
            result["quota_limit"] = payload.get("quota_limit")
            result["quota_used"] = payload.get("quota_used")
            result["error"] = None
            return result

        result["error"] = (
            payload.get("error")
            if isinstance(payload, dict)
            else None
        ) or stderr or stdout[:300] or f"nbs auth whoami failed with exit code {completed.returncode}"

    return result


def _request_platform(platform_url: str, *, timeout: float, context: ssl.SSLContext | None = None) -> dict:
    last_error = None
    for method in ("HEAD", "GET"):
        try:
            req = request.Request(platform_url, headers=PROBE_HEADERS, method=method)
            with request.urlopen(req, timeout=timeout, context=context) as response:
                return {
                    "reachable": 200 <= response.status < 500,
                    "status_code": response.status,
                    "final_url": response.geturl(),
                    "method": method,
                    "error": None,
                }
        except error.HTTPError as exc:
            last_error = exc
            if method == "GET" or exc.code not in (403, 405):
                return {
                    "reachable": 200 <= exc.code < 500,
                    "status_code": exc.code,
                    "final_url": exc.geturl(),
                    "method": method,
                    "error": str(exc),
                }
    raise last_error


def probe_platform(platform_url: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> dict:
    result = {
        "checked": True,
        "reachable": False,
        "tls_verified": True,
        "status_code": None,
        "final_url": platform_url,
        "method": None,
        "error": None,
    }
    try:
        result.update(_request_platform(platform_url, timeout=timeout))
    except error.HTTPError as exc:
        result["reachable"] = 200 <= exc.code < 500
        result["status_code"] = exc.code
        result["final_url"] = exc.geturl()
        result["method"] = exc.headers.get("Allow") or "HEAD"
        result["error"] = str(exc)
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                context = ssl._create_unverified_context()
                result.update(_request_platform(platform_url, timeout=timeout, context=context))
                result["tls_verified"] = False
                result["error"] = "Local Python certificate verification failed; unverified reachability probe succeeded."
                return result
            except Exception as retry_exc:
                result["tls_verified"] = False
                result["error"] = f"{exc}; unverified retry failed: {retry_exc}"
                return result
        result["error"] = str(exc)
    return result


def _candidate_base_urls(platform_url: str, lan_base_url: str, session: dict) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    normalized_lan = normalize_base_url(lan_base_url)
    stored_base_url = normalize_base_url(session.get("base_url"))

    ordered_raws = [os.environ.get("ROIL_BACKEND_BASE_URL")]
    if stored_base_url and stored_base_url != normalized_lan:
        ordered_raws.append(stored_base_url)
    ordered_raws.append(platform_url)
    if stored_base_url == normalized_lan:
        ordered_raws.append(stored_base_url)
    ordered_raws.append(lan_base_url)

    for raw in ordered_raws:
        base_url = normalize_base_url(raw)
        if not base_url or base_url in seen:
            continue
        seen.add(base_url)
        candidates.append(base_url)
    return candidates


def _should_prefer_platform_login(nbs_auth: dict, platform_probe: dict, lan_base_url: str) -> bool:
    if platform_probe.get("reachable") is False:
        return False
    if nbs_auth.get("session_available"):
        return False
    if not nbs_auth.get("auth_file_present") or not nbs_auth.get("access_token_present"):
        return False

    normalized_lan = normalize_base_url(lan_base_url)
    stored_base_url = normalize_base_url(nbs_auth.get("stored_base_url"))
    if stored_base_url and stored_base_url != normalized_lan:
        return False

    for probe in nbs_auth.get("probes") or []:
        if normalize_base_url(probe.get("base_url")) != normalized_lan:
            continue
        if probe.get("valid"):
            return False
        return True
    return False


def _request_json(url: str, *, headers: dict, timeout: float, method: str = "GET", data: bytes | None = None, context: ssl.SSLContext | None = None) -> dict:
    req = request.Request(url, headers=headers, method=method, data=data)
    with request.urlopen(req, timeout=timeout, context=context) as response:
        body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body) if body else {}
        return {
            "status_code": response.status,
            "final_url": response.geturl(),
            "payload": payload,
            "error": None,
        }


def _probe_session(base_url: str, access_token: str, timeout: float) -> dict:
    url = f"{base_url}/api/auth/me"
    result = {
        "checked": True,
        "base_url": base_url,
        "valid": False,
        "reachable": False,
        "tls_verified": True,
        "status_code": None,
        "final_url": url,
        "username": None,
        "quota_remaining": None,
        "quota_limit": None,
        "quota_used": None,
        "error": None,
    }
    headers = {
        **PROBE_HEADERS,
        "Accept": "application/json,text/plain,*/*",
        "Authorization": f"Bearer {access_token}",
    }
    try:
        payload = _request_json(url, headers=headers, timeout=timeout)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload_data = json.loads(body) if body else {}
        except Exception:
            payload_data = {"detail": body[:300] if body else str(exc)}
        payload = {
            "status_code": exc.code,
            "final_url": exc.geturl(),
            "payload": payload_data,
            "error": str(exc),
        }
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                context = ssl._create_unverified_context()
                payload = _request_json(url, headers=headers, timeout=timeout, context=context)
                result["tls_verified"] = False
                result["error"] = "Local Python certificate verification failed; unverified session probe succeeded."
            except error.HTTPError as retry_exc:
                body = retry_exc.read().decode("utf-8", errors="replace")
                try:
                    payload_data = json.loads(body) if body else {}
                except Exception:
                    payload_data = {"detail": body[:300] if body else str(retry_exc)}
                payload = {
                    "status_code": retry_exc.code,
                    "final_url": retry_exc.geturl(),
                    "payload": payload_data,
                    "error": str(retry_exc),
                }
                result["tls_verified"] = False
            except Exception as retry_exc:
                result["tls_verified"] = False
                result["error"] = f"{exc}; unverified retry failed: {retry_exc}"
                return result
        else:
            result["error"] = str(exc)
            return result

    payload_data = payload.get("payload") if isinstance(payload, dict) else {}
    result["status_code"] = payload.get("status_code")
    result["final_url"] = payload.get("final_url") or url
    result["reachable"] = payload.get("status_code") is not None and 200 <= int(payload["status_code"]) < 500
    result["valid"] = bool(payload.get("status_code") == 200 and isinstance(payload_data, dict) and payload_data.get("username"))
    result["username"] = payload_data.get("username") if isinstance(payload_data, dict) else None
    result["quota_remaining"] = payload_data.get("quota_remaining") if isinstance(payload_data, dict) else None
    result["quota_limit"] = payload_data.get("quota_limit") if isinstance(payload_data, dict) else None
    result["quota_used"] = payload_data.get("quota_used") if isinstance(payload_data, dict) else None
    if payload.get("error") and not result["error"]:
        result["error"] = payload["error"]
    return result


def _refresh_session(base_url: str, refresh_token: str, timeout: float) -> dict:
    url = f"{base_url}/api/auth/refresh"
    result = {
        "attempted": True,
        "success": False,
        "base_url": base_url,
        "tls_verified": True,
        "status_code": None,
        "final_url": url,
        "payload": None,
        "error": None,
    }
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    headers = {**PROBE_HEADERS, "Accept": "application/json,text/plain,*/*", "Content-Type": "application/json"}
    try:
        payload = _request_json(url, headers=headers, timeout=timeout, method="POST", data=body)
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            payload_data = json.loads(body_text) if body_text else {}
        except Exception:
            payload_data = {"detail": body_text[:300] if body_text else str(exc)}
        payload = {
            "status_code": exc.code,
            "final_url": exc.geturl(),
            "payload": payload_data,
            "error": str(exc),
        }
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                context = ssl._create_unverified_context()
                payload = _request_json(url, headers=headers, timeout=timeout, method="POST", data=body, context=context)
                result["tls_verified"] = False
                result["error"] = "Local Python certificate verification failed; unverified refresh succeeded."
            except Exception as retry_exc:
                result["tls_verified"] = False
                result["error"] = str(retry_exc)
                return result
        else:
            result["error"] = str(exc)
            return result

    payload_data = payload.get("payload") if isinstance(payload, dict) else {}
    result["status_code"] = payload.get("status_code")
    result["final_url"] = payload.get("final_url") or url
    result["payload"] = payload_data if isinstance(payload_data, dict) else None
    result["success"] = bool(payload.get("status_code") == 200 and isinstance(payload_data, dict) and payload_data.get("access_token"))
    if payload.get("error") and not result["error"]:
        result["error"] = payload["error"]
    return result


def _start_cli_sync(platform_url: str, timeout: float) -> dict:
    base_url = normalize_base_url(platform_url)
    url = f"{base_url}/api/auth/cli/device/start"
    result = {
        "attempted": True,
        "available": False,
        "success": False,
        "base_url": base_url,
        "status_code": None,
        "device_code": None,
        "verification_uri": None,
        "verification_uri_complete": None,
        "user_code": None,
        "expires_in": None,
        "interval": None,
        "error": None,
    }
    headers = {**PROBE_HEADERS, "Accept": "application/json,text/plain,*/*"}
    try:
        payload = _request_json(url, headers=headers, timeout=timeout, method="POST", data=b"")
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            payload_data = json.loads(body_text) if body_text else {}
        except Exception:
            payload_data = {"detail": body_text[:300] if body_text else str(exc)}
        result["status_code"] = exc.code
        result["error"] = str(payload_data.get("detail") if isinstance(payload_data, dict) else payload_data)
        return result
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                context = ssl._create_unverified_context()
                payload = _request_json(url, headers=headers, timeout=timeout, method="POST", data=b"", context=context)
            except Exception as retry_exc:
                result["error"] = f"{exc}; unverified retry failed: {retry_exc}"
                return result
        else:
            result["error"] = str(exc)
            return result

    payload_data = payload.get("payload") if isinstance(payload, dict) else {}
    result["status_code"] = payload.get("status_code")
    if payload.get("status_code") == 200 and isinstance(payload_data, dict):
        result.update(
            {
                "available": True,
                "success": bool(payload_data.get("device_code") and payload_data.get("verification_uri_complete")),
                "device_code": payload_data.get("device_code"),
                "verification_uri": payload_data.get("verification_uri"),
                "verification_uri_complete": payload_data.get("verification_uri_complete"),
                "user_code": payload_data.get("user_code"),
                "expires_in": payload_data.get("expires_in"),
                "interval": payload_data.get("interval"),
                "error": None,
            }
        )
    else:
        result["error"] = payload.get("error") or "CLI sync endpoint did not return a usable device flow payload."
    return result


def _should_offer_cli_sync(nbs_auth: dict, platform_probe: dict) -> bool:
    if nbs_auth.get("session_available"):
        return False
    if platform_probe.get("reachable") is False:
        return False
    return True


def inspect_nbs_auth(platform_url: str, lan_base_url: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> dict:
    session, path = load_auth_session()
    access_token = str(session.get("access_token") or "").strip()
    refresh_token = str(session.get("refresh_token") or "").strip()
    stored_base_url = normalize_base_url(session.get("base_url"))
    candidates = _candidate_base_urls(platform_url, lan_base_url, session)
    result = {
        "auth_file_path": str(path),
        "auth_file_present": path.exists(),
        "access_token_present": bool(access_token),
        "stored_base_url": stored_base_url,
        "candidate_base_urls": candidates,
        "session_available": False,
        "session_base_url": None,
        "session_user": None,
        "quota_remaining": None,
        "quota_limit": None,
        "quota_used": None,
        "probes": [],
        "refresh_probes": [],
        "error": None,
    }
    if result["auth_file_present"] and not session:
        result["error"] = "Auth file exists but could not be parsed."
        return result
    if not access_token:
        return result

    for base_url in candidates:
        probe = _probe_session(base_url, access_token, timeout)
        result["probes"].append(probe)
        if probe["valid"]:
            result["session_available"] = True
            result["session_base_url"] = base_url
            result["session_user"] = probe["username"]
            result["quota_remaining"] = probe["quota_remaining"]
            result["quota_limit"] = probe["quota_limit"]
            result["quota_used"] = probe["quota_used"]
            break

        if refresh_token:
            refresh = _refresh_session(base_url, refresh_token, timeout)
            result["refresh_probes"].append(refresh)
            refresh_payload = refresh.get("payload") or {}
            if refresh.get("success"):
                session["base_url"] = base_url
                session["access_token"] = str(refresh_payload.get("access_token") or "").strip()
                session["refresh_token"] = str(refresh_payload.get("refresh_token") or refresh_token).strip()
                save_auth_session(path, session)
                access_token = session["access_token"]
                refresh_token = session["refresh_token"]
                probe = _probe_session(base_url, access_token, timeout)
                result["probes"].append(probe)
                if probe["valid"]:
                    result["session_available"] = True
                    result["session_base_url"] = base_url
                    result["session_user"] = probe["username"]
                    result["quota_remaining"] = probe["quota_remaining"]
                    result["quota_limit"] = probe["quota_limit"]
                    result["quota_used"] = probe["quota_used"]
                    break

    if result["access_token_present"] and not result["session_available"] and not result["error"]:
        result["error"] = "Stored session token was not accepted by any candidate Roil endpoint."
    return result


def build_status(*, check_platform: bool = True, timeout: float = DEFAULT_PROBE_TIMEOUT) -> dict:
    platform_url = os.environ.get("ROIL_PLATFORM_URL", DEFAULT_PLATFORM_URL).strip() or DEFAULT_PLATFORM_URL
    lan_base_url = normalize_base_url(os.environ.get("ROIL_LAN_BASE_URL") or DEFAULT_LAN_BASE_URL)
    key_status = {name: env_present(name) for name in KEY_NAMES}
    has_base_url = env_present("ROIL_BASE_URL")
    has_fallback_key = any(key_status.values())
    platform_probe = probe_platform(platform_url, timeout) if check_platform else {
        "checked": False,
        "reachable": None,
        "status_code": None,
        "final_url": platform_url,
        "error": None,
    }
    browser_hints = {
        "open_command": shutil.which("open") is not None,
        "xdg_open_command": shutil.which("xdg-open") is not None,
    }
    nbs_cli = detect_nbs_cli()
    nbs_auth = inspect_nbs_auth(platform_url, lan_base_url, timeout)
    if nbs_cli["available"] and not nbs_auth["session_available"]:
        cli_probe = _probe_session_via_cli(str(nbs_cli.get("path") or ""), nbs_auth, timeout)
        nbs_auth["cli_probe"] = cli_probe
        if cli_probe.get("success"):
            nbs_auth["session_available"] = True
            nbs_auth["session_base_url"] = cli_probe.get("base_url")
            nbs_auth["session_user"] = cli_probe.get("username")
            nbs_auth["quota_remaining"] = cli_probe.get("quota_remaining")
            nbs_auth["quota_limit"] = cli_probe.get("quota_limit")
            nbs_auth["quota_used"] = cli_probe.get("quota_used")
            nbs_auth["error"] = None
    else:
        nbs_auth["cli_probe"] = {
            "attempted": False,
            "success": False,
            "base_url": None,
            "username": None,
            "quota_remaining": None,
            "quota_limit": None,
            "quota_used": None,
            "error": None,
        }

    script_dir = Path(__file__).resolve().parent
    draw_script = script_dir / "roil_draw.py"
    if nbs_cli["available"]:
        sync_command = f"{nbs_cli.get('path') or 'nbs'} auth sync-web --base-url {normalize_base_url(platform_url)}"
    else:
        sync_command = f'python3 {draw_script} --prompt "..." --open-platform --json'

    sync_web = {
        "attempted": False,
        "available": False,
        "success": False,
        "base_url": normalize_base_url(platform_url),
        "device_code": None,
        "verification_uri": None,
        "verification_uri_complete": None,
        "user_code": None,
        "expires_in": None,
        "interval": None,
        "command": sync_command,
        "error": None,
    }
    if _should_offer_cli_sync(nbs_auth, platform_probe):
        pending_sync = load_pending_cli_sync(platform_url)
        sync_web.update(pending_sync or _start_cli_sync(platform_url, timeout))
        if sync_web.get("success") and sync_web.get("device_code"):
            save_pending_cli_sync(sync_web)
    if nbs_auth.get("session_available"):
        clear_pending_cli_sync()
    nbs_auth["sync_web"] = sync_web

    if nbs_auth["session_available"] and nbs_cli["available"]:
        recommended_next_step = "generate_via_nbs_cli_backend"
    elif nbs_auth["session_available"]:
        recommended_next_step = "generate_via_platform_backend"
    elif nbs_auth.get("sync_web", {}).get("success"):
        recommended_next_step = "sync_cli_from_browser"
    elif _should_prefer_platform_login(nbs_auth, platform_probe, lan_base_url):
        recommended_next_step = "open_or_login_platform"
    elif nbs_cli["available"]:
        recommended_next_step = "try_nbs_cli_direct"
    elif platform_probe["reachable"] is not False:
        recommended_next_step = "open_or_login_platform"
    else:
        recommended_next_step = "check_network_or_open_platform_manually"
    decision_summary = build_decision_summary(recommended_next_step)
    recommended_commands = build_recommended_commands(platform_url, nbs_cli)
    availability_tier = {
        "generate_via_nbs_cli_backend": "native_cli_backend",
        "generate_via_platform_backend": "platform_backend",
        "sync_cli_from_browser": "needs_cli_sync",
        "try_nbs_cli_direct": "native_cli_direct",
        "open_or_login_platform": "web_handoff",
        "check_network_or_open_platform_manually": "manual_or_none",
    }[recommended_next_step]
    platform_entry_available = bool(platform_probe.get("reachable")) if check_platform else None

    return {
        "skill": "roil-drawing",
        "platform_entry_available": platform_entry_available,
        "platform_url": platform_url,
        "lan_base_url": lan_base_url,
        "platform_probe": platform_probe,
        "roil_base_url_configured": has_base_url,
        "fallback_key_available": has_fallback_key,
        "keys_present": key_status,
        "browser_hints": browser_hints,
        "nbs_cli": nbs_cli,
        "nbs_auth": nbs_auth,
        "safe_to_show_user": {
            "availability_summary": (
                "不要仅因缺少 `roil` 或 `roil-drawing` 独立命令就判定不可用；"
                "当前可用入口应以预检结果里的 `recommended_next_step`、`nbs_cli` 和 `nbs_auth` 为准。"
            ),
            "login_url": platform_url,
            "login_prompt": f"请先登录 Roil 平台：{platform_url}",
            "cli_sync_prompt": (
                "打开授权链接确认后，Roil Drawing 会把浏览器登录态同步成本地会话；不需要安装 nbs。"
            ),
            "no_cli_note": "没有本地 CLI 不等于没有 Roil 出图入口；Roil Web 平台就是有效入口。",
            "fallback_note": (
                "只有在无法使用 Roil 平台且用户接受 fallback 时，才考虑环境中的图片生成 key。"
            ),
        },
        "agent_contract": {
            "must_do_first": [
                recommended_commands["draw"],
                "Only run the preflight command separately when you need to debug login state or routing; do not surface preflight by default before trying to generate.",
            ],
            "must_not_do": [
                "Do not inspect ~/.nbs/auth.json, environment variables, backend source, or alternate image entrypoints before running preflight.",
                "Do not claim there is no Roil drawing entrypoint when `recommended_next_step` points to a native CLI or platform backend path.",
                "Do not ask the user for OPENAI_API_KEY before native CLI and platform handoff paths are exhausted.",
            ],
        },
        "recommended_commands": recommended_commands,
        "availability_tier": availability_tier,
        "decision_summary": decision_summary,
        "recommended_next_step": recommended_next_step,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Roil Drawing entry availability.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--no-platform-check", action="store_true", help="Skip the HTTP reachability probe.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_PROBE_TIMEOUT, help="Platform probe timeout.")
    args = parser.parse_args()

    status = build_status(check_platform=not args.no_platform_check, timeout=args.timeout)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"Roil platform: {status['platform_url']}")
        print("Platform entry available: yes")
        if status["platform_probe"]["checked"]:
            reachable = "yes" if status["platform_probe"]["reachable"] else "no"
            print(f"Platform reachable: {reachable}")
        if status["nbs_auth"]["session_available"]:
            print(
                "Roil session available: "
                f"yes ({status['nbs_auth']['session_user']} @ {status['nbs_auth']['session_base_url']})"
            )
        else:
            print("Roil session available: no")
        print(f"Fallback key available: {'yes' if status['fallback_key_available'] else 'no'}")
        print(status["safe_to_show_user"]["login_prompt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
