from __future__ import annotations

import json
import re
import time
from typing import Any

import requests


WEBHOOK_SITE_REQUESTS_URL = "https://webhook.site/token/{token}/requests"


def is_webhook_site_url(url: str) -> bool:
    return bool(_webhook_site_token(url))


def collect_webhook_site_payloads(
    webhook_url: str,
    request_ids: set[str] | None = None,
    wait_seconds: float = 60,
    poll_interval_seconds: float = 5,
    per_page: int = 100,
    max_pages: int = 10,
    progress_func=None,
) -> list[dict[str, Any]]:
    token = _webhook_site_token(webhook_url)
    if not token:
        raise ValueError("APOLLO_WEBHOOK_URL is not a webhook.site URL")
    request_ids = {str(item) for item in (request_ids or set()) if item}
    deadline = time.monotonic() + max(wait_seconds, 0)
    payloads: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    while True:
        for payload in _fetch_webhook_site_payloads(token, per_page=per_page, max_pages=max_pages):
            request_id = str(payload.get("request_id") or "")
            if request_ids and request_id not in request_ids:
                continue
            event_key = str(payload.get("_webhook_event_key") or request_id or id(payload))
            if event_key in seen_keys:
                continue
            seen_keys.add(event_key)
            payloads.append(payload)
        received_request_ids = {str(payload.get("request_id") or "") for payload in payloads}
        missing_request_ids = request_ids - received_request_ids
        if progress_func:
            progress_func(
                {
                    "received": len(received_request_ids & request_ids) if request_ids else len(payloads),
                    "expected": len(request_ids),
                    "missing": len(missing_request_ids),
                    "payloads": len(payloads),
                    "seconds_remaining": max(int(deadline - time.monotonic()), 0),
                }
            )
        if not request_ids or not missing_request_ids:
            return payloads
        if time.monotonic() >= deadline:
            return payloads
        time.sleep(max(poll_interval_seconds, 1))


def _fetch_webhook_site_payloads(token: str, per_page: int = 100, max_pages: int = 10) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    session = requests.Session()
    for page in range(1, max_pages + 1):
        response = session.get(
            WEBHOOK_SITE_REQUESTS_URL.format(token=token),
            params={"sorting": "newest", "per_page": per_page, "page": page},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        for item in data.get("data") or []:
            payload = _payload_from_request_item(item)
            if payload:
                payloads.append(payload)
        if data.get("is_last_page"):
            break
    return payloads


def _payload_from_request_item(item: dict[str, Any]) -> dict[str, Any] | None:
    content = item.get("content")
    if not content:
        return None
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    payload["_webhook_event_key"] = str(item.get("uuid") or payload.get("request_id") or "")
    payload["_webhook_received_at"] = str(item.get("created_at") or "")
    return payload


def _webhook_site_token(url: str) -> str:
    match = re.search(r"webhook\.site/(?:#!/view/)?([0-9a-fA-F-]{36})", url or "")
    return match.group(1) if match else ""
