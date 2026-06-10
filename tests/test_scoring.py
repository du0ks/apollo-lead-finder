from src.company_scoring import score_company


def test_scores_relevant_company_with_country_and_domain() -> None:
    raw = {
        "name": "Hungary Foundation Construction",
        "description": "Foundation construction, bored piling, and geotechnical engineering contractor",
        "industry": "Civil Engineering",
        "domain": "example.com",
        "country": "Hungary",
    }

    score, reasons = score_company(raw, "foundation construction", "Hungary")

    assert score >= 18
    assert "+5 keyword in company name" in reasons
    assert "+5 country matches" in reasons


def test_penalizes_obviously_irrelevant_company() -> None:
    raw = {
        "name": "Construction Hotel",
        "description": "Hotel and restaurant services",
        "industry": "Hospitality",
        "domain": "hotel.example",
        "country": "Hungary",
    }

    score, reasons = score_company(raw, "construction", "Hungary")

    assert score < 10
    assert "-10 obviously irrelevant" in reasons


def test_company_build_uses_apollo_fallback_fields() -> None:
    from src.company_scoring import build_company

    company = build_company(
        {
            "name": "11-50",
            "domain": "rotaqua.hu",
            "organization_city": "Kovagoszolos",
            "organization_state": "Baranya",
            "organization_country": "Hungary",
            "naics_codes": ["21311"],
            "sic_codes": ["1381"],
        },
        "camp",
        "Hungary",
        "drilling",
    )

    assert company.estimated_num_employees == "11-50"
    assert company.industry == "NAICS: 21311; SIC: 1381"
    assert company.city == "Kovagoszolos"
    assert company.country_from_apollo == "Hungary"
