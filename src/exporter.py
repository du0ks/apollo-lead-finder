from __future__ import annotations

from pathlib import Path

import pandas as pd

from .models import Company, EnrichedLead, Person
from .utils import campaign_output_dir, ensure_dirs


COMPANY_COLUMNS = [
    "campaign_name",
    "country",
    "keyword",
    "company_name",
    "domain",
    "website_url",
    "linkedin_url",
    "industry",
    "estimated_num_employees",
    "city",
    "state",
    "country_from_apollo",
    "apollo_organization_id",
    "description",
    "company_score",
    "score_reasons",
    "search_source",
    "duplicate_key",
]

PEOPLE_COLUMNS = [
    "campaign_name",
    "country",
    "keyword",
    "company_name",
    "domain",
    "apollo_organization_id",
    "person_name",
    "first_name",
    "last_name",
    "title",
    "title_tier",
    "seniority",
    "linkedin_url",
    "city",
    "country_from_apollo",
    "apollo_person_id",
    "ranking_score",
    "ranking_reason",
    "selected_for_enrichment",
    "already_enriched_before",
]

ENRICHED_COLUMNS = [
    column
    for column in PEOPLE_COLUMNS
    if column not in {"selected_for_enrichment", "already_enriched_before"}
] + [
    "email",
    "email_status",
    "phone",
    "enrichment_status",
    "email_source",
]


def export_previews(companies: list[Company], people: list[Person], campaign_name: str) -> dict[str, Path]:
    ensure_dirs()
    output_dir = campaign_output_dir(campaign_name)
    company_path = output_dir / "companies_preview.xlsx"
    people_path = output_dir / "people_preview.xlsx"
    summary_path = output_dir / "campaign_summary.xlsx"

    _write_excel(company_path, [c.model_dump() for c in companies], COMPANY_COLUMNS)
    _write_excel(people_path, [p.model_dump() for p in people], PEOPLE_COLUMNS)

    selected_people = [p for p in people if p.selected_for_enrichment]
    summary = [
        {"metric": "campaign_name", "value": campaign_name},
        {"metric": "companies_exported", "value": len(companies)},
        {"metric": "people_exported", "value": len(people)},
        {"metric": "selected_for_enrichment", "value": len(selected_people)},
        {"metric": "already_enriched_before", "value": sum(1 for p in selected_people if p.already_enriched_before)},
    ]
    pd.DataFrame(summary).to_excel(summary_path, index=False)
    return {"companies": company_path, "people": people_path, "summary": summary_path}


def export_enriched(leads: list[EnrichedLead], campaign_name: str | None = None) -> Path:
    ensure_dirs()
    resolved_campaign = campaign_name or (leads[0].campaign_name if leads else "campaign")
    path = campaign_output_dir(resolved_campaign) / "final_leads_enriched.xlsx"
    _write_excel(path, [lead.model_dump() for lead in leads], ENRICHED_COLUMNS)
    return path


def _write_excel(path: Path, rows: list[dict], columns: list[str]) -> None:
    df = pd.DataFrame(rows)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    df = df[columns]
    df.to_excel(path, index=False)
