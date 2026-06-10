from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Limits(BaseModel):
    max_companies_per_keyword: int = 30
    max_people_per_company: int = 2
    min_company_score: int = 7
    max_total_companies: int = 300
    max_total_people_to_enrich: int = 500

    @field_validator("*")
    @classmethod
    def positive_ints(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limits must be positive integers")
        return value


class TitleConfig(BaseModel):
    tier_1: list[str] = Field(default_factory=list)
    tier_2: list[str] = Field(default_factory=list)
    tier_3: list[str] = Field(default_factory=list)

    def all_titles(self) -> list[str]:
        seen: set[str] = set()
        titles: list[str] = []
        for title in self.tier_1 + self.tier_2 + self.tier_3:
            key = title.strip().lower()
            if key and key not in seen:
                seen.add(key)
                titles.append(title)
        return titles


class CampaignConfig(BaseModel):
    campaign_name: str
    countries: list[str]
    keywords: list[str]
    limits: Limits = Field(default_factory=Limits)
    titles: TitleConfig

    @field_validator("campaign_name")
    @classmethod
    def campaign_name_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign_name is required")
        return value.strip()

    @field_validator("countries", "keywords")
    @classmethod
    def non_empty_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("countries and keywords must not be empty")
        return cleaned


class Company(BaseModel):
    campaign_name: str
    country: str
    keyword: str
    company_name: str = ""
    domain: str = ""
    website_url: str = ""
    linkedin_url: str = ""
    industry: str = ""
    estimated_num_employees: Any = None
    city: str = ""
    state: str = ""
    country_from_apollo: str = ""
    apollo_organization_id: str = ""
    description: str = ""
    company_score: int = 0
    score_reasons: str = ""
    search_source: str = "apollo_organization_search"
    duplicate_key: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class Person(BaseModel):
    campaign_name: str
    country: str
    keyword: str
    company_name: str = ""
    domain: str = ""
    apollo_organization_id: str = ""
    person_name: str = ""
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    title_tier: str = ""
    seniority: str = ""
    linkedin_url: str = ""
    city: str = ""
    country_from_apollo: str = ""
    apollo_person_id: str = ""
    ranking_score: int = 0
    ranking_reason: str = ""
    selected_for_enrichment: bool = False
    already_enriched_before: bool = False
    duplicate_key: str = Field(default="", exclude=True)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class EnrichedLead(Person):
    email: str = ""
    email_status: str = ""
    phone: str = ""
    enrichment_status: str = ""
    email_source: str = ""
    enriched_at: str = ""
    apollo_credit_risk_note: str = ""
