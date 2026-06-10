from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .people_ranking import person_duplicate_key


@dataclass
class WebhookImportSummary:
    files_read: int = 0
    events_imported: int = 0
    records_seen: int = 0
    records_with_email: int = 0
    enrichments_updated: int = 0


def import_webhook_payloads(path: str | Path, db: Database, run_id: str | None = None) -> WebhookImportSummary:
    summary = WebhookImportSummary()
    for file_path in _payload_files(Path(path)):
        payload = _load_json_payload(file_path)
        summary.files_read += 1
        _import_one_payload(payload, db, _event_key(file_path, payload), summary, run_id=run_id)
    return summary


def import_webhook_payload_objects(
    payloads: list[dict[str, Any]],
    db: Database,
    source: str = "webhook",
    run_id: str | None = None,
) -> WebhookImportSummary:
    summary = WebhookImportSummary(files_read=len(payloads))
    for index, payload in enumerate(payloads):
        event_key = str(payload.get("_webhook_event_key") or payload.get("request_id") or f"{source}:{index}:{_payload_hash(payload)}")
        _import_one_payload(payload, db, event_key, summary, run_id=run_id)
    return summary


def _import_one_payload(
    payload: dict[str, Any],
    db: Database,
    event_key: str,
    summary: WebhookImportSummary,
    run_id: str | None = None,
) -> None:
    if db.save_webhook_event(event_key, payload, run_id=run_id):
        summary.events_imported += 1
    for record in _iter_payload_records(payload):
        summary.records_seen += 1
        email, status, source = _email_from_payload(record)
        if not email:
            continue
        summary.records_with_email += 1
        enrichment_key = _resolve_enrichment_key(record, db)
        if not enrichment_key:
            continue
        existing = db.get_enrichment(enrichment_key) or {}
        normalized_record = _normalize_waterfall_record(record, email, status, source)
        merged = _merge_payloads(existing, normalized_record)
        db.save_enrichment(enrichment_key, merged)
        summary.enrichments_updated += 1


def _payload_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(f"Webhook path not found: {path}")
    files = [
        item
        for item in path.rglob("*")
        if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in {"", ".json", ".txt"}
    ]
    return sorted(files)


def _load_json_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Webhook payload file is empty: {path}")
    payload = json.loads(text)
    payload = _unwrap_json(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"Webhook payload must be a JSON object: {path}")
    return payload


def _unwrap_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _unwrap_json(json.loads(stripped))
            except json.JSONDecodeError:
                return value
    if isinstance(value, dict):
        for key in ("body", "content", "payload", "data"):
            nested = value.get(key)
            unwrapped = _unwrap_json(nested)
            if isinstance(unwrapped, (dict, list)) and unwrapped is not nested:
                copy = dict(value)
                copy[key] = unwrapped
                return copy
    return value


def _event_key(path: Path, payload: dict[str, Any]) -> str:
    explicit = payload.get("id") or payload.get("request_id") or payload.get("webhook_request_id")
    if explicit:
        return str(explicit)
    return f"{path.name}:{_payload_hash(payload)}"


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iter_payload_records(payload: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        value = _unwrap_json(value)
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if _looks_like_person_record(value):
            records.append(value)
        for key in ("matches", "people", "contacts", "details", "results", "items", "events"):
            nested = value.get(key)
            if nested is not None:
                visit(nested)
        for key in ("body", "content", "payload", "data"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, str)):
                visit(nested)

    visit(payload)
    return records


def _looks_like_person_record(payload: dict[str, Any]) -> bool:
    person = _person_payload(payload)
    identity = any(person.get(key) for key in ("id", "person_id", "apollo_person_id", "linkedin_url"))
    email, _, _ = _email_from_payload(payload)
    return identity and bool(email)


def _email_from_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    email, status, source = _email_fields(payload)
    if email:
        return email, status, source
    person = _person_payload(payload)
    if person is not payload:
        return _email_fields(person)
    return "", status, source


def _merge_payloads(native: dict, waterfall: dict) -> dict:
    if not waterfall:
        return dict(native)
    merged = dict(native)
    native_person = _person_payload(native)
    waterfall_person = _person_payload(waterfall)
    if native_person is native and waterfall_person is waterfall:
        merged.update({k: v for k, v in waterfall.items() if v not in (None, "", [])})
        return merged
    person = dict(native_person)
    person.update({k: v for k, v in waterfall_person.items() if v not in (None, "", [])})
    merged.update({k: v for k, v in waterfall.items() if v not in (None, "", [])})
    merged["person"] = person
    return merged


def _person_payload(enriched: dict) -> dict:
    for key in ("person", "contact", "match"):
        value = enriched.get(key)
        if isinstance(value, dict):
            return value
    return enriched


def _email_fields(enriched: dict) -> tuple[str, str, str]:
    email = str(enriched.get("email") or "").strip()
    status = str(enriched.get("email_status") or enriched.get("email_verification_status") or "").strip()
    source = "native" if email else "unavailable"
    personal_emails = enriched.get("personal_emails") or enriched.get("personal_email")
    if not email and isinstance(personal_emails, list) and personal_emails:
        email = str(personal_emails[0] or "").strip()
        source = "personal_email"
    elif not email and isinstance(personal_emails, str):
        email = personal_emails.strip()
        source = "personal_email"
    waterfall_email = enriched.get("waterfall_email") or enriched.get("waterfall_email_address")
    if waterfall_email:
        email = str(waterfall_email).strip()
        source = "waterfall"
    email_items = enriched.get("emails")
    if not email and isinstance(email_items, list):
        for item in email_items:
            candidate = item.get("email") if isinstance(item, dict) else item
            if candidate:
                email = str(candidate).strip()
                source = "waterfall"
                break
    if email and not status:
        status = "available"
    return email, status, source


def _normalize_waterfall_record(payload: dict[str, Any], email: str, status: str, source: str) -> dict[str, Any]:
    normalized = dict(payload)
    person = dict(_person_payload(payload))
    email_status = status or "available"
    if source == "waterfall" or "waterfall" in str(payload.get("source") or "").lower():
        person.setdefault("waterfall_email", email)
        normalized.setdefault("waterfall_email", email)
    elif email:
        person.setdefault("waterfall_email", email)
        normalized.setdefault("waterfall_email", email)
    emails = person.get("emails")
    if isinstance(emails, list):
        person["emails"] = emails
    else:
        person["emails"] = [{"email": email, "email_status": email_status}]
    person["email_status"] = email_status
    normalized["email_status"] = email_status
    if person is not payload:
        normalized["person"] = person
    return normalized


def _resolve_enrichment_key(payload: dict[str, Any], db: Database) -> str:
    person = _person_payload(payload)
    apollo_person_id = str(
        person.get("apollo_person_id") or person.get("person_id") or person.get("id") or ""
    ).strip()
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    fallback_key = person_duplicate_key(
        {
            "id": apollo_person_id,
            "apollo_person_id": apollo_person_id,
            "linkedin_url": linkedin_url,
            "person_name": person.get("name") or "",
            "domain": person.get("organization_domain") or person.get("domain") or "",
        }
    )
    return (
        db.find_enrichment_key(apollo_person_id, linkedin_url, fallback_key)
        or db.find_person_duplicate_key(apollo_person_id, linkedin_url, fallback_key)
    )
