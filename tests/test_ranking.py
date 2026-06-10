from src.models import TitleConfig
from src.people_ranking import ranking_score, title_tier


def test_title_tier_matches_case_insensitively() -> None:
    titles = TitleConfig(
        tier_1=["Managing Director"],
        tier_2=["Procurement Manager"],
        tier_3=["Sales Manager"],
    )

    tier, matched = title_tier("regional PROCUREMENT manager", titles)

    assert tier == "tier_2"
    assert matched == "Procurement Manager"


def test_tier_1_scores_above_tier_2_and_3() -> None:
    titles = TitleConfig(tier_1=["CEO"], tier_2=["Project Manager"], tier_3=["Sales Manager"])

    ceo_score, _, _ = ranking_score("CEO", titles)
    project_score, _, _ = ranking_score("Project Manager", titles)
    sales_score, _, _ = ranking_score("Sales Manager", titles)

    assert ceo_score > project_score > sales_score
