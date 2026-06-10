from __future__ import annotations

import logging
import os

from .apollo_client import ApolloClient
from .database import Database
from .models import CampaignConfig, Company, Person
from .people_ranking import build_person, dedupe_people, select_people_for_company
from .utils import normalize_text


logger = logging.getLogger(__name__)


def people_search_email_statuses() -> list[str]:
    raw = os.getenv("APOLLO_PEOPLE_SEARCH_EMAIL_STATUSES", "verified").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def extract_people(response: dict) -> list[dict]:
    people = response.get("people")
    if isinstance(people, list):
        return people
    contacts = response.get("contacts")
    if isinstance(contacts, list):
        return contacts
    return []


def search_people_for_companies(
    config: CampaignConfig,
    companies: list[Company],
    client: ApolloClient,
    db: Database,
    resume: bool = False,
) -> list[Person]:
    all_people: list[Person] = []
    titles = config.titles.all_titles()
    email_statuses = people_search_email_statuses()
    for company in companies:
        if not company.apollo_organization_id and not company.domain:
            logger.info("Skipping people search for %s; no Apollo org id or domain", company.company_name)
            continue
        logger.info("Searching people company=%s", company.company_name)
        response = client.people_search(
            organization_id=company.apollo_organization_id,
            domain=company.domain,
            titles=titles,
            campaign_name=config.campaign_name,
            use_cache=resume,
            email_statuses=email_statuses,
        )
        company_people = [build_person(raw, company, config.titles) for raw in extract_people(response)]
        company_people = [
            p for p in company_people if p.ranking_score > 0 or any(normalize_text(t) in normalize_text(p.title) for t in titles)
        ]
        selected = select_people_for_company(dedupe_people(company_people), config.limits.max_people_per_company)
        for person in selected:
            person.already_enriched_before = db.enrichment_has_usable_email(person.duplicate_key)
            db.upsert_person(config.campaign_name, person.duplicate_key, person.model_dump())
        all_people.extend(selected)
    return dedupe_people(all_people)
