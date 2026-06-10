from src.company_scoring import build_company, dedupe_companies
from src.people_ranking import build_person, dedupe_people, person_duplicate_key
from src.models import TitleConfig


def test_company_dedupe_prefers_domain() -> None:
    first = build_company(
        {"name": "ACME Ltd", "domain": "https://www.acme.com", "country": "Hungary"},
        "camp",
        "Hungary",
        "drilling",
    )
    second = build_company(
        {"name": "ACME Different", "website_url": "acme.com", "country": "Hungary", "description": "drilling"},
        "camp",
        "Hungary",
        "drilling",
    )

    deduped = dedupe_companies([first, second])

    assert len(deduped) == 1
    assert deduped[0].duplicate_key == "domain:acme.com"


def test_person_dedupe_prefers_linkedin() -> None:
    titles = TitleConfig(tier_1=["CEO"], tier_2=[], tier_3=[])
    company = build_company({"name": "ACME", "domain": "acme.com"}, "camp", "Hungary", "drilling")
    first = build_person({"name": "Jane Doe", "title": "CEO", "linkedin_url": "https://linkedin.com/in/jane"}, company, titles)
    second = build_person({"name": "Jane D", "title": "CEO", "linkedin_url": "linkedin.com/in/jane"}, company, titles)

    deduped = dedupe_people([first, second])

    assert len(deduped) == 1
    assert person_duplicate_key(first) == "linkedin:linkedin.com/in/jane"


def test_people_preview_uses_obfuscated_last_name_when_search_hides_surname() -> None:
    titles = TitleConfig(tier_1=["Director"], tier_2=[], tier_3=[])
    company = build_company({"name": "ACME", "domain": "acme.com", "organization_country": "Hungary"}, "camp", "Hungary", "drilling")

    person = build_person({"first_name": "Gabor", "last_name_obfuscated": "Ma***r", "title": "Director"}, company, titles)

    assert person.last_name == "Ma***r"
    assert person.person_name == "Gabor Ma***r"
    assert person.country_from_apollo == "Hungary"
