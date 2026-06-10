from __future__ import annotations

from pathlib import Path

import pandas as pd

from .database import Database
from .enrichment import _merge_enriched
from .exporter import export_enriched
from .models import CampaignConfig, EnrichedLead, Person
from .people_ranking import person_duplicate_key
from .utils import campaign_output_dir


def build_final_leads_from_cache(config: CampaignConfig, db: Database) -> list[EnrichedLead]:
    people_payloads = _latest_people_payloads(config, db)
    leads: list[EnrichedLead] = []
    for payload in people_payloads:
        person = Person(**payload)
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


def rebuild_final_export(config: CampaignConfig, db: Database) -> tuple[Path, list[EnrichedLead]]:
    leads = build_final_leads_from_cache(config, db)
    return export_enriched(leads, config.campaign_name), leads


def _latest_people_payloads(config: CampaignConfig, db: Database) -> list[dict]:
    preview_path = campaign_output_dir(config.campaign_name) / "people_preview.xlsx"
    if preview_path.exists():
        payloads = _people_payloads_from_preview(preview_path, db)
        if payloads:
            return payloads[: config.limits.max_total_people_to_enrich]
    return db.list_selected_person_payloads(
        config.campaign_name,
        limit=config.limits.max_total_people_to_enrich,
    )


def _people_payloads_from_preview(path: Path, db: Database) -> list[dict]:
    df = pd.read_excel(path, dtype=str).fillna("")
    payloads: list[dict] = []
    for row in df.to_dict(orient="records"):
        payload = {key: _clean_value(value) for key, value in row.items()}
        payload["ranking_score"] = int(float(payload.get("ranking_score") or 0))
        payload["selected_for_enrichment"] = _as_bool(payload.get("selected_for_enrichment"))
        payload["already_enriched_before"] = _as_bool(payload.get("already_enriched_before"))
        duplicate_key = person_duplicate_key(
            {
                "apollo_person_id": payload.get("apollo_person_id"),
                "id": payload.get("apollo_person_id"),
                "linkedin_url": payload.get("linkedin_url"),
                "person_name": payload.get("person_name"),
                "domain": payload.get("domain"),
            }
        )
        payload["duplicate_key"] = duplicate_key
        payload["already_enriched_before"] = db.enrichment_has_usable_email(duplicate_key)
        if payload["selected_for_enrichment"]:
            payloads.append(payload)
    payloads.sort(key=lambda item: int(item.get("ranking_score") or 0), reverse=True)
    return payloads


def _clean_value(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else text


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
