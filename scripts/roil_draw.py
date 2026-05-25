#!/usr/bin/env python3
"""Roil Drawing executable entry.

This script gives agents one command to try before falling back to prose:
- Prefer an authenticated Roil/NBS runtime when available.
- Otherwise, try current-environment image generation tools.
- Only prepare a Roil-ready prompt file when no executable image path is available.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib import error, request
import webbrowser

from roil_preflight import build_status, load_auth_session, normalize_base_url, save_auth_session


DEFAULT_MODEL = "gpt-image-2-all"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "low"
DEFAULT_OUTPUT = "output/roil-drawing/roil-drawing.png"
DEFAULT_NBS_TIMEOUT = 420
DEFAULT_PLATFORM_TIMEOUT = 180
DEFAULT_SYNC_TIMEOUT = 120
RUNNER_NAME = "roil-drawing"
PLATFORM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _write_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _maybe_open_platform(platform_url: str) -> bool:
    if not platform_url:
        return False
    if sys.platform == "darwin":
        return subprocess.run(["open", platform_url], check=False).returncode == 0
    if os.name == "nt":
        return bool(webbrowser.open(platform_url))
    elif os.name == "posix":
        return subprocess.run(["xdg-open", platform_url], check=False).returncode == 0
    return bool(webbrowser.open(platform_url))


def _result_payload(
    *,
    success: bool,
    status: str,
    via: str,
    model: str | None = None,
    output_path: str | None = None,
    message: str | None = None,
    **extra: object,
) -> dict:
    payload = {
        "success": success,
        "status": status,
        "via": via,
        "runner": RUNNER_NAME,
        "model": model,
        "output_path": output_path,
        "message": message,
    }
    payload.update(extra)
    return payload


def _prepare_prompt(
    prompt: str,
    out: Path,
    status: dict,
    *,
    open_platform: bool,
    previous_attempt: dict | None = None,
) -> int:
    platform_url = status["platform_url"]
    prompt_path = out.with_suffix(".prompt.txt")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt.strip() + "\n", encoding="utf-8")
    if open_platform:
        _maybe_open_platform(platform_url)
    payload = _result_payload(
        success=False,
        status="needs_platform_login",
        via="roil-web",
        model=None,
        output_path=None,
        message=f"请先登录 Roil 平台：{platform_url}",
        login_required=True,
        login_url=platform_url,
        platform_url=platform_url,
        platform_probe=status.get("platform_probe"),
        prompt_path=str(prompt_path),
    )
    if previous_attempt:
        payload["previous_attempt"] = previous_attempt
    _write_json(payload)
    return 2


def _prepare_cli_sync(
    prompt: str,
    out: Path,
    status: dict,
    *,
    open_platform: bool,
    sync_timeout: int,
    model: str,
    size: str,
    quality: str,
) -> int:
    prompt_path = out.with_suffix(".prompt.txt")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt.strip() + "\n", encoding="utf-8")
    sync_info = (status.get("nbs_auth") or {}).get("sync_web") or {}
    verification_url = str(sync_info.get("verification_uri_complete") or sync_info.get("verification_uri") or "").strip()
    opened_platform = False
    if open_platform and verification_url:
        opened_platform = _maybe_open_platform(verification_url)

    if opened_platform and sync_info.get("device_code"):
        try:
            session = _poll_cli_sync(
                str(sync_info.get("base_url") or status.get("platform_url") or ""),
                str(sync_info.get("device_code") or ""),
                timeout_seconds=sync_timeout,
                interval_seconds=int(sync_info.get("interval") or 2),
            )
            next_status = dict(status)
            next_auth = dict(status.get("nbs_auth") or {})
            next_auth.update(
                {
                    "session_available": True,
                    "session_base_url": session.get("base_url"),
                    "session_user": session.get("username"),
                }
            )
            next_status["nbs_auth"] = next_auth
            platform_result = _run_platform_generate(prompt, out, next_status, model=model, size=size, quality=quality)
            platform_result["synced_via"] = "web_device_flow"
            platform_result["prompt_path"] = str(prompt_path)
            _write_json(platform_result)
            return 0 if platform_result.get("success") else 4
        except Exception as exc:
            sync_info = dict(sync_info)
            sync_info["poll_error"] = str(exc)

    payload = _result_payload(
        success=False,
        status="needs_cli_sync",
        via="nbs-cli-web-sync",
        model=None,
        output_path=None,
        message="需要从已登录的 Roil 浏览器页面同步本地会话。请打开授权链接确认；不需要安装 nbs。",
        login_required=False,
        cli_sync_required=True,
        verification_url=verification_url,
        user_code=sync_info.get("user_code"),
        expires_in=sync_info.get("expires_in"),
        sync_command=sync_info.get("command"),
        sync_poll_error=sync_info.get("poll_error"),
        prompt_path=str(prompt_path),
    )
    _write_json(payload)
    return 2


def _generate_openai(prompt: str, out: Path, *, model: str, size: str, quality: str) -> int:
    try:
        from openai import OpenAI
    except ImportError:
        _write_json(
            _result_payload(
                success=False,
                status="missing_dependency",
                via="openai-image-api",
                model=model,
                output_path=None,
                message="缺少 openai Python 包；请先安装 openai，或登录 Roil Web 平台继续。",
                dependency="openai",
            )
        )
        return 3

    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    client = OpenAI()
    try:
        result = client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            quality=quality,
            n=1,
        )
    except Exception as exc:
        _write_json(
            _result_payload(
                success=False,
                status="image_api_error",
                via="openai-image-api",
                model=model,
                output_path=None,
                message=str(exc),
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
        )
        return 4

    image_b64 = result.data[0].b64_json
    out.write_bytes(base64.b64decode(image_b64))
    _write_json(
        _result_payload(
            success=True,
            status="generated",
            via="openai-image-api",
            model=model,
            output_path=str(out),
            message=f"Image generated via openai-image-api using {model}.",
            elapsed_seconds=round(time.time() - started, 2),
        )
    )
    return 0


def _parse_json(text: str) -> dict | None:
    payload = str(text or "").strip()
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else {"data": data}


def _request_json(
    base_url: str,
    path: str,
    *,
    access_token: str | None = None,
    json_body: dict | None = None,
    timeout: int = DEFAULT_PLATFORM_TIMEOUT,
) -> dict:
    url = f"{normalize_base_url(base_url)}{path}"
    data = None
    headers = dict(PLATFORM_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    req = request.Request(url, headers=headers, data=data, method="POST" if json_body is not None else "GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            return payload if isinstance(payload, dict) else {"data": payload}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {"detail": body[:500] if body else str(exc)}
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc


def _post_json_status(base_url: str, path: str, json_body: dict, *, timeout: int = 60) -> tuple[int, dict]:
    url = f"{normalize_base_url(base_url)}{path}"
    headers = {**PLATFORM_HEADERS, "Content-Type": "application/json"}
    req = request.Request(
        url,
        headers=headers,
        data=json.dumps(json_body).encode("utf-8"),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            return int(response.status), payload if isinstance(payload, dict) else {"data": payload}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {"detail": body[:500] if body else str(exc)}
        return int(exc.code), payload if isinstance(payload, dict) else {"detail": str(payload)}


def _poll_cli_sync(base_url: str, device_code: str, *, timeout_seconds: int, interval_seconds: int) -> dict:
    normalized_base_url = normalize_base_url(base_url)
    if not normalized_base_url:
        raise RuntimeError("Missing Roil platform base URL for browser sync.")
    if not device_code:
        raise RuntimeError("Missing browser sync device code.")

    _, auth_path = load_auth_session()
    deadline = time.time() + max(1, int(timeout_seconds or DEFAULT_SYNC_TIMEOUT))
    interval = max(1, int(interval_seconds or 2))
    last_detail = ""

    while time.time() < deadline:
        status_code, payload = _post_json_status(
            normalized_base_url,
            "/api/auth/cli/device/poll",
            {"device_code": device_code},
            timeout=30,
        )
        if status_code == 200:
            session = {
                "base_url": normalize_base_url(payload.get("base_url") or normalized_base_url),
                "username": payload.get("username"),
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token"),
                "token_type": payload.get("token_type", "bearer"),
            }
            if not session["access_token"] or not session["refresh_token"]:
                raise RuntimeError("Roil browser sync returned an incomplete session.")
            save_auth_session(auth_path, session)
            return session

        detail = str(payload.get("detail") or payload.get("error") or payload).strip()
        last_detail = detail or last_detail
        if status_code == 428:
            time.sleep(interval)
            continue
        if status_code == 410:
            raise RuntimeError("Roil browser sync request expired.")
        raise RuntimeError(detail or f"Roil browser sync failed with HTTP {status_code}.")

    raise RuntimeError(last_detail or "Timed out waiting for browser approval.")


def _refresh_platform_session(session: dict, auth_path: Path, base_url: str) -> dict:
    refresh_token = str(session.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("Stored Roil session has no refresh token.")
    payload = _request_json(
        base_url,
        "/api/auth/refresh",
        json_body={"refresh_token": refresh_token},
        timeout=60,
    )
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Roil refresh returned no access token.")
    session["base_url"] = normalize_base_url(base_url)
    session["access_token"] = access_token
    session["refresh_token"] = str(payload.get("refresh_token") or refresh_token).strip()
    session["token_type"] = str(payload.get("token_type") or session.get("token_type") or "bearer")
    save_auth_session(auth_path, session)
    return session


def _download_platform_file(base_url: str, source_url: str, out: Path) -> None:
    source = str(source_url or "").strip()
    if not source:
        raise RuntimeError("Roil backend returned an empty image URL.")
    if source.startswith(("http://", "https://")):
        url = source
    else:
        url = f"{normalize_base_url(base_url)}/{source.lstrip('/')}"
    req = request.Request(url, headers=PLATFORM_HEADERS, method="GET")
    out.parent.mkdir(parents=True, exist_ok=True)
    with request.urlopen(req, timeout=DEFAULT_PLATFORM_TIMEOUT) as response:
        out.write_bytes(response.read())


def _run_platform_generate(prompt: str, out: Path, status: dict, *, model: str, size: str, quality: str) -> dict:
    auth_info = status.get("nbs_auth") or {}
    base_url = normalize_base_url(auth_info.get("session_base_url") or status.get("platform_url"))
    if not base_url:
        return _result_payload(
            success=False,
            status="platform_backend_unavailable",
            via="roil-platform-backend",
            model=model,
            output_path=None,
            message="No Roil platform base URL is available.",
        )

    session, auth_path = load_auth_session()
    access_token = str(session.get("access_token") or "").strip()
    if not access_token:
        return _result_payload(
            success=False,
            status="platform_session_missing",
            via="roil-platform-backend",
            model=model,
            output_path=None,
            message="No stored Roil access token is available.",
        )

    body = {
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "style": "standard",
        "subject": "general",
        "model": model,
    }
    started = time.time()
    try:
        try:
            payload = _request_json(base_url, "/api/generate/single", access_token=access_token, json_body=body)
        except RuntimeError as exc:
            if "401" not in str(exc) and "token" not in str(exc).lower() and "unauthorized" not in str(exc).lower():
                raise
            session = _refresh_platform_session(session, auth_path, base_url)
            access_token = str(session.get("access_token") or "").strip()
            payload = _request_json(base_url, "/api/generate/single", access_token=access_token, json_body=body)

        image_urls = payload.get("urls") or ([payload.get("url")] if payload.get("url") else [])
        if not image_urls:
            raise RuntimeError("Roil backend image generation returned no URL.")
        _download_platform_file(base_url, str(image_urls[0]), out)
    except Exception as exc:
        return _result_payload(
            success=False,
            status="platform_backend_error",
            via="roil-platform-backend",
            model=model,
            output_path=None,
            message=str(exc),
            error_type=exc.__class__.__name__,
            error=str(exc),
        )

    return _result_payload(
        success=True,
        status="generated",
        via="roil-platform-backend",
        model=payload.get("actual_model") or model,
        output_path=str(out),
        message=f"Image generated via Roil platform backend using {payload.get('actual_model') or model}.",
        image_url=image_urls[0],
        requested_model=model,
        attempted_models=payload.get("attempted_models"),
        fallback_used=payload.get("fallback_used"),
        remaining_quota=payload.get("remaining_quota"),
        elapsed_seconds=round(time.time() - started, 2),
    )


def _prepare_auth_override(auth_info: dict) -> tuple[str | None, str | None]:
    auth_path = str(auth_info.get("auth_file_path") or "").strip()
    if not auth_info.get("auth_file_present") or not auth_path:
        return None, None

    session_base_url = normalize_base_url(auth_info.get("session_base_url"))
    stored_base_url = normalize_base_url(auth_info.get("stored_base_url"))
    if not session_base_url or session_base_url == stored_base_url:
        return auth_path, None

    source_path = Path(auth_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Stored auth payload is not a JSON object.")
    payload["base_url"] = session_base_url

    tmp = tempfile.NamedTemporaryFile(prefix="roil_nbs_auth_", suffix=".json", delete=False)
    with tmp:
        tmp.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return tmp.name, tmp.name


def _run_nbs_generate(prompt: str, out: Path, status: dict, *, model: str, size: str, quality: str, force_direct: bool) -> dict:
    cli_info = status.get("nbs_cli") or {}
    cli_path = str(cli_info.get("path") or "").strip()
    if not cli_path:
        return _result_payload(
            success=False,
            status="nbs_cli_unavailable",
            via="nbs-cli-direct" if force_direct else "nbs-cli-backend",
            model=model,
            output_path=None,
            message="Current runtime did not expose an NBS CLI entry.",
        )

    env = os.environ.copy()
    auth_cleanup_path = None
    via = "nbs-cli-direct" if force_direct else "nbs-cli-backend"

    if force_direct:
        env["NBS_FORCE_DIRECT"] = "1"
    else:
        auth_info = status.get("nbs_auth") or {}
        try:
            auth_path, auth_cleanup_path = _prepare_auth_override(auth_info)
        except Exception as exc:
            return _result_payload(
                success=False,
                status="nbs_auth_override_failed",
                via=via,
                model=model,
                output_path=None,
                message=str(exc),
            )
        if auth_path:
            env["NBS_AUTH_FILE"] = auth_path

    cmd = [
        cli_path,
        "image",
        "generate",
        "--prompt",
        prompt,
        "--model",
        model,
        "--size",
        size,
        "--quality",
        quality,
        "--output",
        str(out),
        "--json",
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=DEFAULT_NBS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _result_payload(
            success=False,
            status="nbs_cli_timeout",
            via=via,
            model=model,
            output_path=None,
            message=f"NBS image generation timed out after {DEFAULT_NBS_TIMEOUT}s.",
            error=str(exc),
        )
    finally:
        if auth_cleanup_path:
            Path(auth_cleanup_path).unlink(missing_ok=True)

    payload = _parse_json(completed.stdout)
    if payload is None:
        payload = _result_payload(
            success=False,
            status="nbs_cli_invalid_output",
            via=via,
            model=model,
            output_path=None,
            message="NBS CLI returned non-JSON output.",
            stdout=completed.stdout.strip()[:1000],
            stderr=completed.stderr.strip()[:1000],
        )

    payload.setdefault("success", completed.returncode == 0)
    payload.setdefault("status", "generated" if completed.returncode == 0 else "nbs_cli_error")
    payload.setdefault("via", via)
    payload.setdefault("runner", RUNNER_NAME)
    payload.setdefault("model", payload.get("actual_model") or payload.get("model") or model)
    payload.setdefault("output_path", payload.get("output_path") or str(out) if completed.returncode == 0 else None)
    if completed.returncode == 0:
        payload.setdefault("message", f"Image generated via {via}.")
    else:
        payload["success"] = False
        payload.setdefault("status", "nbs_cli_error")
        payload.setdefault("message", payload.get("error") or completed.stderr.strip() or completed.stdout.strip() or "NBS CLI generation failed.")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate with Roil Drawing or prepare Roil Web prompt.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--open-platform", action="store_true")
    parser.add_argument("--sync-timeout", type=int, default=DEFAULT_SYNC_TIMEOUT)
    parser.add_argument("--json", action="store_true", help="Reserved for symmetry; output is always JSON.")
    args = parser.parse_args()

    status = build_status()
    out = Path(args.out)

    if status.get("recommended_next_step") == "sync_cli_from_browser":
        return _prepare_cli_sync(
            args.prompt,
            out,
            status,
            open_platform=args.open_platform,
            sync_timeout=args.sync_timeout,
            model=args.model,
            size=args.size,
            quality=args.quality,
        )

    if status.get("recommended_next_step") == "open_or_login_platform":
        return _prepare_prompt(
            args.prompt,
            out,
            status,
            open_platform=args.open_platform,
        )

    if status.get("recommended_next_step") == "generate_via_platform_backend" or (
        status.get("nbs_auth", {}).get("session_available") and not status.get("nbs_cli", {}).get("available")
    ):
        platform_result = _run_platform_generate(
            args.prompt,
            out,
            status,
            model=args.model,
            size=args.size,
            quality=args.quality,
        )
        _write_json(platform_result)
        return 0 if platform_result.get("success") else 4

    if status.get("nbs_auth", {}).get("session_available") and status.get("nbs_cli", {}).get("available"):
        backend_result = _run_nbs_generate(
            args.prompt,
            out,
            status,
            model=args.model,
            size=args.size,
            quality=args.quality,
            force_direct=False,
        )
        if backend_result.get("success"):
            _write_json(backend_result)
            return 0

        direct_result = _run_nbs_generate(
            args.prompt,
            out,
            status,
            model=args.model,
            size=args.size,
            quality=args.quality,
            force_direct=True,
        )
        if direct_result.get("success"):
            direct_result.setdefault("fallback_from", "nbs-cli-backend")
            _write_json(direct_result)
            return 0

        backend_result["direct_attempt"] = direct_result
        _write_json(backend_result)
        return 4

    if status.get("nbs_cli", {}).get("available"):
        direct_result = _run_nbs_generate(
            args.prompt,
            out,
            status,
            model=args.model,
            size=args.size,
            quality=args.quality,
            force_direct=True,
        )
        if direct_result.get("success"):
            _write_json(direct_result)
            return 0

        if status.get("platform_probe", {}).get("reachable") is not False:
            return _prepare_prompt(
                args.prompt,
                out,
                status,
                open_platform=args.open_platform,
                previous_attempt=direct_result,
            )

        _write_json(direct_result)
        return 4

    return _prepare_prompt(args.prompt, out, status, open_platform=args.open_platform)


if __name__ == "__main__":
    raise SystemExit(main())
