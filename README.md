# Apollo Lead Finder

A Python CLI that finds B2B leads through the Apollo.io API: it searches companies by country and keyword, finds the right people inside them by job title, and exports Excel files, while asking for confirmation before any step that can spend Apollo credits.

It uses the official Apollo REST API only. No scraping, no browser automation.

## The problem

Apollo charges credits for organization search and for revealing contact details, and it is easy to burn through them with a broad search or by enriching the same person twice. I wanted a tool that searches wide and cheap, lets me review the shortlist in Excel, and only then enriches a limited number of people, with every credit-spending step shown up front and confirmed by hand.

## How it works

1. Load a campaign config (YAML or Excel): countries, keywords, limits and three tiers of target job titles.
2. Run Apollo Organization Search for every country × keyword pair, with pagination. Raw JSON responses are saved per campaign.
3. Deduplicate companies by domain, then LinkedIn URL, then normalized name + country.
4. Score each company (keyword in name or description, relevant industry terms, country match, penalties for unrelated industries such as hotels or banks) and drop those under `min_company_score`.
5. Run People Search on the kept companies with the configured titles. By default it only asks for people whose email Apollo marks as verified.
6. Rank people by title tier (tier 1 = 100, tier 2 = 70, tier 3 = 40, +5 for senior roles) and select up to `max_people_per_company` per company.
7. Export `companies_preview.xlsx`, `people_preview.xlsx` and `campaign_summary.xlsx`.
8. Only with `--enrich-selected`: enrich the selected people with Bulk People Enrichment (max 10 per call), fall back to single-person enrichment when there is no email match, and optionally request Apollo's waterfall email enrichment via a webhook. Results go to `final_leads_enriched.xlsx`.

Everything is cached in SQLite (`data/apollo_leads.sqlite`): identical searches are reused with `--resume`, and a person is never enriched twice (matched by LinkedIn URL, Apollo ID, or name + company domain). People who were enriched before and came back without an email are not retried unless you pass `--retry-unavailable-enrichments`.

### Credit guard

Before Organization Search and before enrichment, `src/credit_guard.py` prints:

- the operation and how many API calls or people it may use credits for
- how many are cached or skipped
- the configured limit (`max_total_companies`, `max_total_people_to_enrich`)
- the current credit balance, if a balance endpoint is configured (`APOLLO_CREDITS_PATH`), and a warning if the estimate exceeds it

The run continues only if you type exactly `YES`. Afterwards it prints credits before and after (when available) and counts of successful, skipped and failed records. If enrichment stops halfway (for example when credits run out), completed results are saved and a partial Excel file is written, so a rerun with `--resume --enrich-selected` picks up where it stopped.

API calls are rate-limited and retried with exponential backoff (up to 5 attempts) on retryable HTTP errors.

## Tech stack

Python 3.11, `requests`, `tenacity`, `pydantic`, `pandas` + `openpyxl`, `PyYAML`, `python-dotenv`, SQLite, `pytest`.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set APOLLO_API_KEY
```

Other `.env` settings (all documented in `.env.example`):

- `APOLLO_BASE_URL`: defaults to `https://api.apollo.io/api/v1`
- `APOLLO_CREDITS_PATH`: leave empty unless your account has a credit balance endpoint
- `APOLLO_WEBHOOK_URL`: public HTTPS URL for waterfall enrichment callbacks (a `webhook.site` URL works for testing)
- `APOLLO_PEOPLE_SEARCH_EMAIL_STATUSES=verified`: People Search filter; empty disables it
- `APOLLO_SINGLE_ENRICH_FALLBACK=true`: try single-person enrichment when bulk finds no email
- `APOLLO_WATERFALL_POLL_ATTEMPTS`, `APOLLO_WEBHOOK_WAIT_SECONDS`, `APOLLO_WEBHOOK_POLL_INTERVAL_SECONDS`: webhook polling and waiting

## Commands

```bash
# check the config (no API calls)
python -m src.main validate --config config/campaign.example.yaml

# check config and .env, e.g. that the API key isn't still the placeholder (no API calls)
python -m src.main doctor --config config/campaign.example.yaml

# show planned searches and the maximum number of API calls (no API calls)
python -m src.main run --config config/campaign.example.yaml --dry-run

# search and export previews; reuse cached searches with --resume
python -m src.main run --config config/campaign.example.yaml
python -m src.main run --config config/campaign.example.yaml --resume

# enrich the selected people (asks for YES first)
python -m src.main run --config config/campaign.example.yaml --resume --enrich-selected
python -m src.main run --config config/campaign.example.yaml --resume --enrich-selected --retry-unavailable-enrichments

# import Apollo webhook payloads saved as JSON files
python -m src.main import-webhooks --path data/input/webhooks

# pull payloads straight from the webhook.site URL in .env, optionally rebuilding the final Excel
python -m src.main collect-webhooks --config config/campaign.example.yaml [--wait-seconds 60]

# rebuild final_leads_enriched.xlsx from the local cache
python -m src.main export-final --config config/campaign.example.yaml

# reconcile local state with the preview files and rebuild the final export
python -m src.main repair --config config/campaign.example.yaml [--wait-seconds 60]

# show database, cache and output counts for a campaign
python -m src.main status --config config/campaign.example.yaml
```

`validate`, `doctor`, `--dry-run`, `import-webhooks`, `collect-webhooks`, `export-final`, `repair` and `status` make no Apollo API calls.

## Campaign config

See `config/campaign.example.yaml` (a Poland campaign for drilling and foundation-construction equipment) and `config/titles.example.yaml`. Fields: `campaign_name`, `countries`, `keywords`, `limits` (`max_companies_per_keyword`, `max_people_per_company`, `min_company_score`, `max_total_companies`, `max_total_people_to_enrich`) and `titles.tier_1` / `tier_2` / `tier_3`.

Excel configs use the sheets `campaign`, `countries`, `keywords`, `limits` (`key`, `value`), and `titles_tier_1` to `titles_tier_3` (column `title`).

## Output

```
data/output/<campaign_name>/
  companies_preview.xlsx
  people_preview.xlsx
  campaign_summary.xlsx
  final_leads_enriched.xlsx
  raw/                      raw Apollo responses
data/apollo_leads.sqlite    cache, campaign runs, enrichment history
logs/app.log, logs/api_calls.log
```

Use a new `campaign_name` when you change countries, keywords or titles, to get a clean output folder.

## Tests

```bash
pytest
```

The tests cover the API client, credit guard, deduplication, company scoring, people ranking, enrichment, webhook import, campaign state and the doctor checks.

## Limitations

- Credit balance can only be shown if your Apollo account exposes a balance endpoint; otherwise the confirmation is based on the estimate alone.
- With my API key Apollo rejects the `/webhook_result` polling endpoint, so waterfall results have to come in through the webhook (`collect-webhooks` or `import-webhooks`).
- Company scoring uses a fixed list of industry terms aimed at construction and drilling (`src/company_scoring.py`); other industries need that list changed.
- It is a CLI only. A Telegram bot on top of the same modules is a possible next step but is not built.
