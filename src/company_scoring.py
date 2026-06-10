from __future__ import annotations

from typing import Any

from .models import Company
from .utils import normalize_domain, normalize_text, normalize_url


RELEVANT_INDUSTRY_TERMS = {
    "construction",
    "machinery",
    "civil engineering",
    "mining",
    "building materials",
    "geotechnical",
    "architecture",
    "engineering",
    "industrial machinery",
    "foundation",
    "tunnel",
    "drilling",
}

IRRELEVANT_TERMS = {
    "restaurant",
    "hotel",
    "casino",
    "fashion",
    "cosmetic",
    "software",
    "advertising",
    "bank",
    "insurance",
    "school",
    "hospital",
}


def company_duplicate_key(company: dict[str, Any] | Company, fallback_country: str = "") -> str:
    data = company.model_dump() if isinstance(company, Company) else company
    domain = normalize_domain(data.get("domain") or data.get("primary_domain") or data.get("website_url"))
    if domain:
        return f"domain:{domain}"
    linkedin = normalize_url(data.get("linkedin_url"))
    if linkedin:
        return f"linkedin:{linkedin}"
    name = normalize_text(data.get("company_name") or data.get("name"))
    country = normalize_text(data.get("country_from_apollo") or data.get("country") or fallback_country)
    return f"name_country:{name}:{country}"


def score_company(company: dict[str, Any], keyword: str, expected_country: str) -> tuple[int, list[str]]:
    name = normalize_text(company.get("name") or company.get("company_name"))
    keyword_norm = normalize_text(keyword)
    description = normalize_text(company.get("description") or company.get("short_description"))
    industry = normalize_text(_industry_value(company))
    raw_country = company.get("country") or company.get("organization_country") or company.get("country_from_apollo")
    country = normalize_text(raw_country or expected_country)
    expected_country_norm = normalize_text(expected_country)
    domain = normalize_domain(company.get("domain") or company.get("primary_domain") or company.get("website_url"))

    score = 0
    reasons: list[str] = []

    if keyword_norm and keyword_norm in name:
        score += 5
        reasons.append("+5 keyword in company name")
    if keyword_norm and keyword_norm in description:
        score += 3
        reasons.append("+3 keyword in description")
    if any(term in industry or term in description for term in RELEVANT_INDUSTRY_TERMS):
        score += 3
        reasons.append("+3 relevant industry/description")
    if domain:
        score += 2
        reasons.append("+2 website/domain exists")
    if expected_country_norm and expected_country_norm == country:
        score += 5
        if raw_country:
            reasons.append("+5 country matches")
        else:
            reasons.append("+5 country matches search filter")
    elif expected_country_norm and not country:
        reasons.append("country unavailable")

    if any(term in " ".join([name, description, industry]) for term in IRRELEVANT_TERMS):
        score -= 10
        reasons.append("-10 obviously irrelevant")

    return score, reasons


def build_company(raw: dict[str, Any], campaign_name: str, country: str, keyword: str) -> Company:
    score, reasons = score_company(raw, keyword, country)
    company = Company(
        campaign_name=campaign_name,
        country=country,
        keyword=keyword,
        company_name=raw.get("name") or raw.get("company_name") or "",
        domain=normalize_domain(raw.get("domain") or raw.get("primary_domain") or raw.get("website_url")),
        website_url=raw.get("website_url") or raw.get("website") or "",
        linkedin_url=raw.get("linkedin_url") or raw.get("linkedin_url_normalized") or "",
        industry=_industry_value(raw),
        estimated_num_employees=_employee_value(raw),
        city=raw.get("organization_city") or raw.get("city") or "",
        state=raw.get("organization_state") or raw.get("state") or "",
        country_from_apollo=raw.get("organization_country") or raw.get("country") or country,
        apollo_organization_id=raw.get("organization_id") or raw.get("id") or "",
        description=raw.get("description") or raw.get("short_description") or "",
        company_score=score,
        score_reasons="; ".join(reasons),
        raw=raw,
    )
    company.duplicate_key = company_duplicate_key(company, country)
    return company


def _industry_value(raw: dict[str, Any]) -> str:
    industry = raw.get("industry") or raw.get("industry_name") or raw.get("primary_industry")
    if industry:
        return str(industry)
    codes: list[str] = []
    if raw.get("naics_codes"):
        codes.append(f"NAICS: {', '.join(map(str, raw['naics_codes']))}")
    if raw.get("sic_codes"):
        codes.append(f"SIC: {', '.join(map(str, raw['sic_codes']))}")
    return "; ".join(codes)


def _employee_value(raw: dict[str, Any]) -> Any:
    for key in ("estimated_num_employees", "organization_estimated_num_employees", "employee_count", "num_employees"):
        if raw.get(key) is not None:
            return raw.get(key)
    name = str(raw.get("name") or "")
    if name.replace("-", "").isdigit() and "-" in name:
        return name
    return None


def dedupe_companies(companies: list[Company]) -> list[Company]:
    best: dict[str, Company] = {}
    for company in companies:
        current = best.get(company.duplicate_key)
        if current is None or company.company_score > current.company_score:
            best[company.duplicate_key] = company
    return list(best.values())
