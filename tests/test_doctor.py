from pathlib import Path

from src.doctor import run_doctor


def test_doctor_reports_placeholder_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path / "output")
    config = tmp_path / "campaign.yaml"
    config.write_text(
        """
campaign_name: poland_test
countries:
  - Poland
keywords:
  - drilling
limits:
  max_companies_per_keyword: 10
  max_people_per_company: 2
  min_company_score: 7
  max_total_companies: 100
  max_total_people_to_enrich: 20
titles:
  tier_1:
    - CEO
  tier_2: []
  tier_3: []
""",
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text(
        "APOLLO_API_KEY=your_apollo_api_key_here\nAPOLLO_BASE_URL=https://api.apollo.io/api/v1\n",
        encoding="utf-8",
    )

    result = run_doctor(str(config), env)

    assert result.ok is False
    assert any("placeholder" in line for line in result.lines)
    assert any("No Apollo API calls" in line for line in result.lines)
