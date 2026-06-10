# Apollo Lead Finder

Production-ready Python MVP for an internal Apollo-based lead finding pipeline. It searches companies by country and keyword, searches people inside selected companies by title, ranks/selects the best contacts, exports preview Excel files, and only enriches people when explicitly requested.

This project uses the official Apollo API only. It does not scrape Apollo and does not use browser automation.

## What It Does

1. Reads a campaign config from YAML or Excel.
2. Runs Apollo Organization Search for each country and keyword.
3. Saves raw API JSON responses in `data/output/<campaign_name>/raw/`.
4. Deduplicates companies by domain, then LinkedIn URL, then normalized name plus country.
5. Scores company relevance and keeps companies above `min_company_score`.
6. Runs Apollo People Search against selected companies and configured target titles.
7. Ranks people by title tier and selects `max_people_per_company`.
8. Exports:
   - `data/output/<campaign_name>/companies_preview.xlsx`
   - `data/output/<campaign_name>/people_preview.xlsx`
   - `data/output/<campaign_name>/campaign_summary.xlsx`
9. Optionally enriches selected people only with `--enrich-selected` and exact `YES` confirmation.
10. Stores cache and history in `data/apollo_leads.sqlite`.

## Apollo Credit Guardrails

- Organization Search consumes credits.
- Organization Enrichment consumes credits and is not used by default.
- People API Search is used for net new people and Apollo docs say it does not consume credits.
- People Enrichment / Bulk People Enrichment may consume credits.
- Bulk People Enrichment is limited to 10 people per API call.
- This CLI searches broadly but enriches narrowly.
- Enrichment never happens automatically.

## Credit Approval And Balance Checks

The tool must protect Apollo credits. It should never run a credit-consuming step silently.

Before any step that may consume credits, the CLI or future Telegram bot should show:

- The operation type, for example Organization Search, People Enrichment, or Bulk People Enrichment.
- The estimated number of records affected.
- The estimated credit risk, for example "up to X people may be enriched".
- The configured safety limit, for example `max_total_people_to_enrich`.
- The current Apollo credit balance if Apollo exposes that value through the API.
- A warning if the estimated operation may exceed available credits or configured limits.

The user must explicitly approve before the operation starts:

```text
This operation may consume Apollo credits.
Estimated selected people: 120
Estimated credit risk: up to 120 people enrichment credits, depending on Apollo billing rules.
Configured limit: 500
Current Apollo credits: 4,000
Continue? Type YES:
```

If the user does not type exactly `YES`, the operation must stop.

After a credit-consuming operation, the tool should show:

- Credits before the operation, if available.
- Credits after the operation, if available.
- Estimated or observed credits used.
- Number of successful enrichments.
- Number of skipped records because they were already enriched.
- Number of failed records.

If Apollo does not provide credit balance through the API, the tool should say that clearly and ask the user to confirm based on the estimate. The local cache still helps reduce wasted credits by avoiding duplicate enrichments.

## Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```bash
APOLLO_API_KEY=your_real_apollo_api_key
APOLLO_BASE_URL=https://api.apollo.io/api/v1
APOLLO_CREDITS_PATH=
APOLLO_WEBHOOK_URL=
APOLLO_PEOPLE_SEARCH_EMAIL_STATUSES=verified
APOLLO_SINGLE_ENRICH_FALLBACK=true
APOLLO_WATERFALL_POLL_ATTEMPTS=0
APOLLO_WEBHOOK_WAIT_SECONDS=60
APOLLO_WEBHOOK_POLL_INTERVAL_SECONDS=5
```

Leave `APOLLO_CREDITS_PATH` empty unless Apollo provides/enables a credit balance endpoint for your account. When it is configured, the CLI will try to show credits before and after credit-risk operations.

Leave `APOLLO_WEBHOOK_URL` empty unless you have a public webhook URL ready for Apollo waterfall enrichment callbacks.

`APOLLO_PEOPLE_SEARCH_EMAIL_STATUSES=verified` makes People Search prefer contacts Apollo already marks as verified. This matches the working Globby people-search behavior and reduces the number of selected people that later enrich as unavailable. Leave it empty only if you want broader search results with lower email coverage.

`APOLLO_SINGLE_ENRICH_FALLBACK=true` makes the CLI try single-person People Enrichment for selected contacts that Bulk People Enrichment could not email-match.

`APOLLO_WATERFALL_POLL_ATTEMPTS=0` disables Apollo webhook-result polling. Your current API key can request waterfall enrichment, but Apollo rejects `/webhook_result/{request_id}` for that key. Use webhook.site JSON import instead.

When `APOLLO_WEBHOOK_URL` is a `webhook.site` URL, `--enrich-selected` collects webhook.site callbacks automatically after submitting enrichment requests. `APOLLO_WEBHOOK_WAIT_SECONDS` controls how long enrichment waits for Apollo's delayed waterfall responses.

## Run

Validate config:

```bash
python -m src.main validate --config config/campaign.example.yaml
```

Dry run without Apollo calls:

```bash
python -m src.main run --config config/campaign.example.yaml --dry-run
```

Run preview only:

```bash
python -m src.main run --config config/campaign.example.yaml
```

Resume using cached identical searches where available:

```bash
python -m src.main run --config config/campaign.example.yaml --resume
```

Run enrichment:

```bash
python -m src.main run --config config/campaign.example.yaml --enrich-selected
```

The enrichment command prints a credit warning and requires typing exactly `YES`. It also respects `max_total_people_to_enrich` so a single run cannot enrich more people than the configured limit.

By default, people that were already enriched before and came back with no email are not retried, because retrying them can spend Apollo lead/email credits again. To explicitly retry unavailable/no-email records, use:

```bash
python -m src.main run --config config/campaign.example.yaml --resume --enrich-selected --retry-unavailable-enrichments
```

Enrichment uses Apollo Bulk People Enrichment with personal email reveal. If a bulk result still has no email, the CLI also tries single-person People Enrichment. If `APOLLO_WEBHOOK_URL` is configured, both paths request Apollo waterfall email enrichment. Apollo requires this webhook to be a publicly accessible HTTPS URL.

For quick testing, you can create a temporary URL with a service such as `webhook.site`, then set:

```bash
APOLLO_WEBHOOK_URL=https://webhook.site/your-unique-url
```

For production, use your own HTTPS endpoint instead of a third-party webhook inbox because Apollo may send contact data to that URL. Apollo can still return unavailable emails because of source coverage, account limits, GDPR/privacy restrictions, or no matching email.

If you see a message like `api/v1/webhook_result/show is not accessible with this api_key`, that is Apollo blocking the optional polling endpoint for your key. It does not prove the webhook failed. Keep `APOLLO_WATERFALL_POLL_ATTEMPTS=0`, receive results in webhook.site, then import the saved JSON files.

Run enrichment with cached searches and waterfall email enabled:

```bash
python -m src.main run --config config/campaign.example.yaml --resume --enrich-selected
```

If you use `webhook.site`, wait a few minutes after enrichment and save Apollo webhook JSON payloads into:

```text
data/input/webhooks/
```

Then import those webhook payloads into the local SQLite cache:

```bash
python -m src.main import-webhooks --path data/input/webhooks
```

Or collect directly from webhook.site using the URL in `.env`:

```bash
python -m src.main collect-webhooks --config config/campaign.example.yaml
```

Rebuild the final Excel from local data only:

```bash
python -m src.main export-final --config config/campaign.example.yaml
```

`import-webhooks` and `export-final` do not call Apollo and do not spend credits. They only read local JSON files and the SQLite cache.

## Config

Edit `config/campaign.example.yaml` or create a new YAML config with:

- `campaign_name`
- `countries`
- `keywords`
- `limits`
- `titles.tier_1`
- `titles.tier_2`
- `titles.tier_3`

The example campaign targets Hungary with drilling, bored piling, anchoring, tunnel construction, geotechnical engineering, and foundation construction keywords.

## Excel Config

YAML is recommended. Excel configs are also supported with these sheets:

- `campaign`: one row with `campaign_name`
- `countries`: column `country`
- `keywords`: column `keyword`
- `limits`: columns `key`, `value`
- `titles_tier_1`: column `title`
- `titles_tier_2`: column `title`
- `titles_tier_3`: column `title`

## Logs And Cache

- App log: `logs/app.log`
- API call log: `logs/api_calls.log`
- SQLite database: `data/apollo_leads.sqlite`
- Campaign Excel outputs: `data/output/<campaign_name>/`
- Campaign raw API JSON: `data/output/<campaign_name>/raw/`

The cache helps avoid repeating identical organization/people searches when using `--resume`, and avoids enriching the same person twice based on LinkedIn URL, Apollo ID, or normalized name plus company domain.

## Campaign Folders

Each campaign writes its files to a separate folder based on `campaign_name`. For example:

```text
data/output/hungary_test2/
  companies_preview.xlsx
  people_preview.xlsx
  campaign_summary.xlsx
  final_leads_enriched.xlsx
  raw/

data/output/poland_test/
  companies_preview.xlsx
  people_preview.xlsx
  campaign_summary.xlsx
  final_leads_enriched.xlsx
  raw/
```

Use a new `campaign_name` when you change country, keywords, filters, or title strategy and want a clean output folder.

## Tests

```bash
pytest
```

## Telegram Bot Phase 2

After the CLI is reliable, the Telegram bot can wrap these same modules instead of duplicating logic. The bot commands will create and update campaign configs, call the preview pipeline, show summaries, and trigger the same explicit enrichment confirmation flow:

- `/new_campaign`
- `/set_countries`
- `/set_keywords`
- `/set_titles`
- `/preview`
- `/export_preview`
- `/enrich_selected`
- `/export_final`

The bot should call the CLI service functions already in `src/` and preserve the same Apollo credit guardrails. Any Telegram command that may spend credits must use `src/credit_guard.py` to show the estimated credit impact, current credit balance if available, and require an explicit confirmation message before continuing.
