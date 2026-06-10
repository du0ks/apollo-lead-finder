from src.apollo_client import ApolloAPIError, ApolloClient
from src.error_handling import explain_exception


def test_extract_credit_balance_from_nested_payload() -> None:
    payload = {"usage": {"email_credits_remaining": "123"}}

    assert ApolloClient._extract_credit_balance(payload) == 123


def test_organization_search_payload_clamps_per_page_to_apollo_limit() -> None:
    client = object.__new__(ApolloClient)

    payload = client.organization_search_payload("Hungary", "drilling", per_page=500, page=2)

    assert payload["per_page"] == ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE
    assert payload["page"] == 2


def test_cloudflare_522_is_retryable() -> None:
    assert ApolloClient._is_retryable_status(522)
    assert ApolloClient._is_retryable_status(524)
    assert not ApolloClient._is_retryable_status(422)


def test_html_error_body_is_cleaned_to_title() -> None:
    body = "<html><head><title>apollo.io | 522: Connection timed out</title></head><body>long html</body></html>"

    assert ApolloClient._clean_error_body(body) == "apollo.io | 522: Connection timed out"


def test_people_search_can_filter_verified_email_status() -> None:
    captured = {}
    client = object.__new__(ApolloClient)

    def fake_cached_post(endpoint, payload, campaign_name, use_cache):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"people": []}

    client._cached_post = fake_cached_post

    client.people_search(
        organization_id="org-1",
        domain="example.com",
        titles=["CEO"],
        campaign_name="camp",
        email_statuses=["verified"],
    )

    assert captured["endpoint"] == ApolloClient.PEOPLE_SEARCH_PATH
    assert captured["payload"]["contact_email_status"] == ["verified"]
    assert captured["payload"]["include_similar_titles"] is True


def test_raw_api_json_is_saved_under_campaign_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path)
    client = object.__new__(ApolloClient)

    client._save_raw("/people/match", "abcdef123456", {"ok": True}, "Poland Test")

    expected = tmp_path / "poland_test" / "raw"
    assert expected.exists()
    assert list(expected.glob("*_people_match_abcdef1234.json"))


def test_unauthorized_apollo_error_explains_key_not_credits() -> None:
    exc = ApolloAPIError(
        "Apollo API error 401 while calling /mixed_companies/search: Invalid access credentials.",
        status_code=401,
        endpoint="/mixed_companies/search",
        response_body='{"error":"Invalid access credentials."}',
    )

    message = explain_exception(exc, "Organization Search").format()

    assert "Apollo rejected the API key" in message
    assert "not a credit-balance problem" in message
    assert "APOLLO_API_KEY" in message
    assert "/mixed_companies/search" in message


def test_placeholder_api_key_is_rejected() -> None:
    client = object.__new__(ApolloClient)
    client.api_key = "your_apollo_api_key_here"

    try:
        client.validate_api_key()
    except ValueError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("placeholder API key should be rejected")


def test_insufficient_credits_error_is_explained_as_credit_problem() -> None:
    exc = ApolloAPIError(
        "Apollo API error 422 while calling /people/bulk_match: "
        "{\"error\":\"You have insufficient credits! Upgrade your plan to increase your number of lead credits.\"}",
        status_code=422,
        endpoint="/people/bulk_match",
    )

    message = explain_exception(exc, "Bulk People Enrichment").format()

    assert "credits are insufficient" in message
    assert "separate from search credits" in message
    assert "lower limits.max_total_people_to_enrich" in message
