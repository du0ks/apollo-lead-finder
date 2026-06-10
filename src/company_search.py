from __future__ import annotations

import math
import logging

from .apollo_client import ApolloClient
from .company_scoring import build_company, dedupe_companies
from .database import Database
from .models import CampaignConfig, Company


logger = logging.getLogger(__name__)


def extract_organizations(response: dict) -> list[dict]:
    results: list[dict] = []
    accounts = response.get("accounts")
    if isinstance(accounts, list):
        results.extend(accounts)
    orgs = response.get("organizations")
    if isinstance(orgs, list):
        results.extend(orgs)
    return results


def search_companies(config: CampaignConfig, client: ApolloClient, db: Database, resume: bool = False) -> list[Company]:
    companies: list[Company] = []
    page_size = ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE
    max_pages = math.ceil(config.limits.max_companies_per_keyword / page_size)
    for country in config.countries:
        for keyword in config.keywords:
            logger.info("Searching organizations country=%s keyword=%s", country, keyword)
            found_for_keyword = 0
            for page in range(1, max_pages + 1):
                response = client.organization_search(
                    country=country,
                    keyword=keyword,
                    per_page=config.limits.max_companies_per_keyword,
                    page=page,
                    campaign_name=config.campaign_name,
                    use_cache=resume,
                )
                raw_results = extract_organizations(response)
                for raw in raw_results:
                    if found_for_keyword >= config.limits.max_companies_per_keyword:
                        break
                    company = build_company(raw, config.campaign_name, country, keyword)
                    db.upsert_company(config.campaign_name, company.duplicate_key, company.model_dump())
                    companies.append(company)
                    found_for_keyword += 1

                pagination = response.get("pagination") or {}
                total_pages = pagination.get("total_pages")
                if not raw_results or (isinstance(total_pages, int) and page >= total_pages):
                    break
                if len(raw_results) < page_size:
                    break

    deduped = dedupe_companies(companies)
    filtered = [c for c in deduped if c.company_score >= config.limits.min_company_score]
    filtered.sort(key=lambda c: c.company_score, reverse=True)
    return filtered[: config.limits.max_total_companies]
