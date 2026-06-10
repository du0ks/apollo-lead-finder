from __future__ import annotations

import logging
import os
import time
from typing import Any

from .apollo_client import ApolloAPIError, ApolloClient
from .database import Database
from .models import EnrichedLead, Person
from .utils import chunked, utc_now_iso
from .webhook_importer import import_webhook_payload_objects
from .webhook_site import collect_webhook_site_payloads, is_webhook_site_url


logger = logging.getLogger(__name__)
CREDIT_NOTE = "People Enrichment/Bulk People Enrichment may consume Apollo credits. Search-only preview does not enrich."
UNAVAILABLE_AFTER_ALL_METHODS = "unavailable_after_native_and_waterfall"


class EnrichmentStoppedError(RuntimeError):
    def __init__(
        self,
        message: str,
        partial_leads: list[EnrichedLead],
        selected_people: list[Person],
        original_error: ApolloAPIError,
    ) -> None:
        super().__init__(message)
        self.partial_leads = partial_leads
        self.selected_people = selected_people
        self.original_error = original_error


def selected_people_for_enrichment(people: list[Person], max_total: int) -> list[Person]:
    selected = [p for p in people if p.selected_for_enrichment and not p.already_enriched_before]
    selected.sort(key=lambda p: p.ranking_score, reverse=True)
    return selected[:max_total]


def people_needing_enrichment(
    people: list[Person],
    db: Database,
    max_total: int,
    retry_unavailable: bool = False,
) -> list[Person]:
    selected = [p for p in people if p.selected_for_enrichment]
    selected.sort(key=lambda p: p.ranking_score, reverse=True)
    selected = selected[:max_total]
    if retry_unavailable:
        return [person for person in selected if not db.enrichment_has_usable_email(person.duplicate_key)]
    return [person for person in selected if not db.has_enrichment(person.duplicate_key)]


def estimate_enrichment_risk(people: list[Person]) -> str:
    calls = (len(people) + 9) // 10
    return f"Estimated selected people: {len(people)}. Bulk enrichment API calls: {calls}. This may consume Apollo credits."


def enrich_selected_people(
    people: list[Person],
    client: ApolloClient,
    db: Database,
    campaign_name: str,
    max_total: int,
    progress_func=print,
    retry_unavailable: bool = False,
    run_id: str | None = None,
) -> list[EnrichedLead]:
    selected = [p for p in people if p.selected_for_enrichment]
    selected.sort(key=lambda p: p.ranking_score, reverse=True)
    selected = selected[:max_total]
    to_enrich = people_needing_enrichment(selected, db, max_total, retry_unavailable=retry_unavailable)
    to_enrich_keys = {person.duplicate_key for person in to_enrich}
    leads: list[EnrichedLead] = []
    waterfall_request_ids: set[str] = set()
    if progress_func:
        progress_func(
            f"Enrichment plan: selected={len(selected)}, already have usable email={len(selected) - len(to_enrich)}, "
            f"needs enrichment={len(to_enrich)}"
        )
    for person in selected:
        if person.duplicate_key in to_enrich_keys:
            continue
        cached = db.get_enrichment(person.duplicate_key)
        if cached:
            leads.append(_merge_enriched(person, cached, "cached_enrichment", email_source="cache"))

    batches = chunked(to_enrich, 10)
    for batch_number, batch in enumerate(batches, start=1):
        if progress_func:
            progress_func(f"Bulk enrichment batch {batch_number}/{len(batches)}: submitting {len(batch)} people")
        request_people = [_person_enrichment_payload(person) for person in batch]
        try:
            response = client.enrich_people_bulk(request_people, campaign_name)
        except ApolloAPIError as exc:
            _collect_webhook_site_waterfall_results(client, db, waterfall_request_ids, progress_func=progress_func, run_id=run_id)
            partial = _build_final_leads_from_people(selected, db)
            raise EnrichmentStoppedError(
                f"Enrichment stopped during bulk batch {batch_number}/{len(batches)}: {exc}",
                partial,
                selected,
                exc,
            ) from exc
        request_id = _extract_request_id(response)
        if request_id:
            waterfall_request_ids.add(request_id)
        enriched_items = _extract_enriched_people(response)
        waterfall_items = _poll_waterfall_email_results(response, client, campaign_name)
        for index, person in enumerate(batch):
            enriched = enriched_items[index] if index < len(enriched_items) else {}
            waterfall = _find_matching_waterfall_item(person, waterfall_items, index)
            merged = _merge_payloads(enriched, waterfall)
            if _single_enrich_fallback_enabled() and not _payload_has_email(merged):
                try:
                    if progress_func:
                        progress_func(
                            f"Single-person fallback: {person.person_name or person.apollo_person_id} "
                            "had no immediate email; trying Apollo /people/match"
                        )
                    single_response = client.enrich_person(_person_enrichment_payload(person), campaign_name)
                    single_request_id = _extract_request_id(single_response)
                    if single_request_id:
                        waterfall_request_ids.add(single_request_id)
                    merged = _merge_payloads(merged, single_response)
                except ApolloAPIError as exc:
                    if _is_insufficient_credit_error(exc):
                        merged["apollo_person_id"] = person.apollo_person_id
                        db.save_enrichment(person.duplicate_key, merged)
                        _collect_webhook_site_waterfall_results(
                            client,
                            db,
                            waterfall_request_ids,
                            progress_func=progress_func,
                            run_id=run_id,
                        )
                        partial = _build_final_leads_from_people(selected, db)
                        raise EnrichmentStoppedError(
                            f"Enrichment stopped during single-person fallback: {exc}",
                            partial,
                            selected,
                            exc,
                        ) from exc
                    logger.warning("Single-person enrichment fallback failed for %s: %s", person.apollo_person_id, exc)
            merged["apollo_person_id"] = person.apollo_person_id
            db.save_enrichment(person.duplicate_key, merged)
            leads.append(_merge_enriched(person, merged, "enriched"))
    _collect_webhook_site_waterfall_results(client, db, waterfall_request_ids, progress_func=progress_func, run_id=run_id)
    if waterfall_request_ids:
        return _build_final_leads_from_people(selected, db)
    return leads


def _person_enrichment_payload(person: Person) -> dict:
    payload: dict = {}
    if person.apollo_person_id:
        payload["id"] = person.apollo_person_id
    if person.linkedin_url:
        payload["linkedin_url"] = person.linkedin_url
    if person.first_name:
        payload["first_name"] = person.first_name
    if person.last_name and not _is_obfuscated(person.last_name):
        payload["last_name"] = person.last_name
    if person.domain:
        payload["organization_domain"] = person.domain
    return payload


def _extract_enriched_people(response: dict) -> list[dict]:
    for key in ("matches", "people", "contacts", "details"):
        value = response.get(key)
        if isinstance(value, list):
            return value
    if isinstance(response.get("person"), dict):
        return [response["person"]]
    return []


def _merge_enriched(person: Person, enriched: dict, status: str, email_source: str = "") -> EnrichedLead:
    enriched_person = _person_payload(enriched)
    email, email_status, inferred_source = _email_fields(enriched_person)
    if not email:
        email_status = email_status or UNAVAILABLE_AFTER_ALL_METHODS
        status = UNAVAILABLE_AFTER_ALL_METHODS if status == "enriched" else status
    phones = enriched_person.get("phone_numbers") or []
    phone = ""
    if isinstance(phones, list) and phones:
        first = phones[0]
        phone = (first.get("raw_number") or first.get("sanitized_number")) if isinstance(first, dict) else str(first)
    first_name = enriched_person.get("first_name") or person.first_name
    last_name = enriched_person.get("last_name") or person.last_name
    person_name = enriched_person.get("name") or " ".join([first_name, last_name]).strip() or person.person_name
    lead_data = person.model_dump()
    lead_data.update(
        {
            "person_name": person_name,
            "first_name": first_name,
            "last_name": last_name,
            "linkedin_url": enriched_person.get("linkedin_url") or person.linkedin_url,
            "email": email,
            "email_status": email_status,
            "phone": phone or enriched_person.get("phone") or "",
            "enrichment_status": status,
            "email_source": email_source or inferred_source,
            "enriched_at": utc_now_iso(),
            "apollo_credit_risk_note": CREDIT_NOTE,
        }
    )
    return EnrichedLead(**lead_data)


def _poll_waterfall_email_results(response: dict, client: ApolloClient, campaign_name: str) -> list[dict]:
    request_id = _extract_request_id(response)
    if not request_id:
        return []
    attempts = int(os.getenv("APOLLO_WATERFALL_POLL_ATTEMPTS", "0"))
    if attempts <= 0:
        return []
    delay_seconds = float(os.getenv("APOLLO_WATERFALL_POLL_DELAY_SECONDS", "10"))
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay_seconds)
        try:
            result = client.poll_webhook_result(request_id, campaign_name)
        except ApolloAPIError as exc:
            logger.warning("Skipping waterfall polling for request_id=%s: %s", request_id, exc)
            return []
        items = _extract_enriched_people(result)
        if items:
            return items
        if _is_terminal_waterfall_result(result):
            return []
    return []


def _extract_request_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("request_id", "webhook_request_id"):
            value = payload.get(key)
            if value:
                return str(value)
        for value in payload.values():
            found = _extract_request_id(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_request_id(item)
            if found:
                return found
    return ""


def _is_terminal_waterfall_result(payload: dict) -> bool:
    status = str(payload.get("status") or payload.get("state") or "").lower()
    return status in {"failed", "failure", "completed", "complete", "not_found", "not found", "unavailable"}


def _find_matching_waterfall_item(person: Person, waterfall_items: list[dict], index: int) -> dict:
    if not waterfall_items:
        return {}
    for item in waterfall_items:
        item_person = _person_payload(item)
        if person.apollo_person_id and person.apollo_person_id in {
            str(item_person.get("id") or ""),
            str(item_person.get("person_id") or ""),
            str(item_person.get("apollo_person_id") or ""),
        }:
            return item
        if person.linkedin_url and item_person.get("linkedin_url") == person.linkedin_url:
            return item
    return waterfall_items[index] if index < len(waterfall_items) else {}


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


def _payload_has_email(payload: dict) -> bool:
    for candidate in (payload, _person_payload(payload)):
        email, _, _ = _email_fields(candidate)
        if email:
            return True
    return False


def _single_enrich_fallback_enabled() -> bool:
    value = os.getenv("APOLLO_SINGLE_ENRICH_FALLBACK", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _is_insufficient_credit_error(exc: ApolloAPIError) -> bool:
    text = f"{exc} {exc.response_body}".lower()
    return "insufficient credits" in text or "increase your number of lead credits" in text


def _collect_webhook_site_waterfall_results(
    client: ApolloClient,
    db: Database,
    request_ids: set[str],
    progress_func=print,
    run_id: str | None = None,
) -> None:
    if not request_ids or not client.webhook_url or not is_webhook_site_url(client.webhook_url):
        return
    wait_seconds = float(os.getenv("APOLLO_WEBHOOK_WAIT_SECONDS", "60"))
    poll_interval = float(os.getenv("APOLLO_WEBHOOK_POLL_INTERVAL_SECONDS", "5"))
    if progress_func:
        progress_func(
            f"Waiting for Apollo waterfall callbacks on webhook.site: expected={len(request_ids)}, "
            f"timeout={int(wait_seconds)}s, poll_interval={int(poll_interval)}s"
        )

    def report_webhook_progress(state: dict) -> None:
        if progress_func:
            progress_func(
                "Waterfall callback progress: "
                f"received={state['received']}/{state['expected']}, "
                f"payloads={state['payloads']}, "
                f"missing={state['missing']}, "
                f"seconds_remaining={state['seconds_remaining']}"
            )

    try:
        payloads = collect_webhook_site_payloads(
            client.webhook_url,
            request_ids=request_ids,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval,
            progress_func=report_webhook_progress,
        )
    except Exception as exc:
        logger.warning("Could not collect webhook.site waterfall results automatically: %s", exc)
        return
    summary = import_webhook_payload_objects(payloads, db, source="webhook.site", run_id=run_id)
    logger.info(
        "Imported webhook.site waterfall results: payloads=%s records_with_email=%s enrichments_updated=%s",
        len(payloads),
        summary.records_with_email,
        summary.enrichments_updated,
    )
    if progress_func:
        progress_func(
            "Waterfall import complete: "
            f"payloads={len(payloads)}, records_with_email={summary.records_with_email}, "
            f"cache_updates={summary.enrichments_updated}"
        )


def _build_final_leads_from_people(people: list[Person], db: Database) -> list[EnrichedLead]:
    leads: list[EnrichedLead] = []
    for person in people:
        enrichment = db.get_enrichment(person.duplicate_key)
        if enrichment:
            leads.append(_merge_enriched(person, enrichment, "cached_enrichment"))
            continue
        lead_data = person.model_dump()
        lead_data.update(
            {
                "email": "",
                "email_status": "not_enriched",
                "phone": "",
                "enrichment_status": "not_enriched",
                "email_source": "unavailable",
            }
        )
        leads.append(EnrichedLead(**lead_data))
    return leads


def _is_obfuscated(value: str) -> bool:
    return "***" in value
