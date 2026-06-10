from src.company_search import extract_organizations


def test_extract_organizations_falls_back_to_accounts_when_organizations_empty() -> None:
    response = {
        "organizations": [],
        "accounts": [{"name": "Rotaqua", "organization_id": "org-1"}],
    }

    assert extract_organizations(response) == [{"name": "Rotaqua", "organization_id": "org-1"}]


def test_extract_organizations_combines_accounts_and_organizations() -> None:
    response = {
        "accounts": [{"name": "Account Company", "organization_id": "org-1"}],
        "organizations": [{"name": "Organization Company", "id": "org-2"}],
    }

    assert extract_organizations(response) == [
        {"name": "Account Company", "organization_id": "org-1"},
        {"name": "Organization Company", "id": "org-2"},
    ]
