from __future__ import annotations

from typing import Any

from .models import Person, TitleConfig
from .utils import normalize_domain, normalize_text, normalize_url


TIER_BASE_SCORE = {"tier_1": 100, "tier_2": 70, "tier_3": 40}


def title_tier(title: str, title_config: TitleConfig) -> tuple[str, str]:
    normalized = normalize_text(title)
    for tier_name in ("tier_1", "tier_2", "tier_3"):
        for target in getattr(title_config, tier_name):
            target_norm = normalize_text(target)
            if target_norm and target_norm in normalized:
                return tier_name, target
    return "", ""


def ranking_score(title: str, title_config: TitleConfig, seniority: str = "") -> tuple[int, str, str]:
    tier, matched = title_tier(title, title_config)
    if not tier:
        return 0, "", "no configured title match"
    score = TIER_BASE_SCORE[tier]
    reason = f"{tier} match: {matched}"
    if normalize_text(seniority) in {"owner", "founder", "c suite", "c-level", "director", "manager"}:
        score += 5
        reason += "; seniority boost"
    return score, tier, reason


def person_duplicate_key(person: dict[str, Any] | Person, company_domain: str = "") -> str:
    data = person.model_dump() if isinstance(person, Person) else person
    linkedin = normalize_url(data.get("linkedin_url"))
    if linkedin:
        return f"linkedin:{linkedin}"
    apollo_id = data.get("apollo_person_id") or data.get("id")
    if apollo_id:
        return f"apollo_id:{apollo_id}"
    name = normalize_text(data.get("person_name") or data.get("name"))
    domain = normalize_domain(data.get("domain") or company_domain)
    return f"name_domain:{name}:{domain}"


def build_person(raw: dict[str, Any], company: Any, title_config: TitleConfig) -> Person:
    score, tier, reason = ranking_score(raw.get("title") or "", title_config, raw.get("seniority") or "")
    last_name = raw.get("last_name") or raw.get("last_name_obfuscated") or ""
    person_name = raw.get("name") or " ".join([raw.get("first_name", ""), last_name]).strip()
    person = Person(
        campaign_name=company.campaign_name,
        country=company.country,
        keyword=company.keyword,
        company_name=company.company_name,
        domain=company.domain,
        apollo_organization_id=company.apollo_organization_id,
        person_name=person_name,
        first_name=raw.get("first_name") or "",
        last_name=last_name,
        title=raw.get("title") or "",
        title_tier=tier,
        seniority=raw.get("seniority") or "",
        linkedin_url=raw.get("linkedin_url") or "",
        city=raw.get("city") or company.city,
        country_from_apollo=raw.get("country") or company.country_from_apollo or company.country,
        apollo_person_id=raw.get("id") or raw.get("person_id") or "",
        ranking_score=score,
        ranking_reason=reason,
        raw=raw,
    )
    person.duplicate_key = person_duplicate_key(person, company.domain)
    return person


def dedupe_people(people: list[Person]) -> list[Person]:
    best: dict[str, Person] = {}
    for person in people:
        current = best.get(person.duplicate_key)
        if current is None or person.ranking_score > current.ranking_score:
            best[person.duplicate_key] = person
    return list(best.values())


def select_people_for_company(people: list[Person], max_people: int) -> list[Person]:
    matched = [p for p in people if p.ranking_score > 0]
    matched.sort(key=lambda p: p.ranking_score, reverse=True)
    for person in matched[:max_people]:
        person.selected_for_enrichment = True
    return matched
