# Data sources: how to get each file without tracking anyone

ReadSharp's privacy policy promises no cookies, analytics or third-party scripts on the website and no analytics in the app. Every source below is an aggregate count that ReadSharp's providers already produce. No file may contain names, email addresses, IP addresses, or user or customer IDs.

## The short way: the export script
In the ReadSharp repository, `python scripts/marketing/export_weekly.py --out <folder>` writes every file below except `search_ads.csv` and `creator_posts.csv`. It reads its credentials from environment variables, uses them read-only and never prints them; a source whose variables are missing is skipped with a note:

| Source | Variables | Files |
|---|---|---|
| Cloudflare | `CLOUDFLARE_API_TOKEN` (Analytics: Read on the zone), `CLOUDFLARE_ZONE_ID` | `cloudflare_requests.csv` |
| Supabase | `READSHARP_REPORTING_DB_URL` (the `reporting_reader` login) | `supabase_signups.csv`, `cohorts.csv`, `ai_usage.csv` |
| Stripe | `STRIPE_RESTRICTED_KEY` (a restricted `rk_live_` key with read access only) | `stripe_events.csv`, `stripe_fees.csv` |
| Google Search Console | `GSC_SERVICE_ACCOUNT_FILE`, optional `GSC_PROPERTY` (default `sc-domain:readsharp.app`) | `search_console.csv`, `search_queries.csv` |
| Releases | GitHub CLI and `CHANGELOG.md` | `releases.csv` |

Daily files are merged with the copies already in the folder, so weekly runs keep history longer than Cloudflare retains it. Run it into the same folder every week.

## Source-tagged download links
Every Download button points at a short link that the site's `_redirects` file sends (302) to the current Windows installer. The site build writes that file from the version it links, so redeploying the site after each release moves every link to the new installer. The link a person clicked is counted in Cloudflare's server-side logs, with no cookie involved.

| Link | Placed on |
|---|---|
| `/get/site` | download buttons on readsharp.app |
| `/get/google-ads` | `/lp/google`, the landing page used only as the Google Ads final URL |
| `/get/microsoft-ads` | `/lp/bing`, the landing page used only as the Microsoft Ads final URL |
| `/get/email` | download links in emails ReadSharp sends |
| `/get/c-<creator>` | `/c/<creator>`, a landing page each creator links to (add the slug to `site/creators.json`) |
| `/e`, `/s` | the "Email me the link" email and the "Send to my PC" link; both go to `/download` |

The `/lp/` and `/c/` pages are `noindex` and stay out of the navigation and sitemap. Turn off ad click-ID auto-tagging (gclid / msclkid); it is not needed. ReadSharp's checkout does not accept promotion codes, so creators are measured by their `/get/c-` link alone.

## Files

### cloudflare_requests.csv: `date,path,requests` (optional `host,status,device_type,os`)
Cloudflare edge analytics are recorded at the server and need no script. Avoid Cloudflare Web Analytics: it adds a JavaScript beacon, which would break the no-scripts promise.
- Export script: GraphQL Analytics API, dataset `httpRequestsAdaptiveGroups`, GET requests by day, host, path and status for readsharp.app and downloads.readsharp.app. The `settings` node decides whether device type and OS are included and how far back data goes on the plan. End-user requests only (`requestSource`), where the plan offers it, so the keep-alive Worker's checks are left out.
- Kept paths (an allowlist): one-level pages, `/lp/*`, `/c/*`, `/get/*`, `/e`, `/s` on the site; installers, `.blockmap` files and `latest.yml` on the download host. Paths with anything like an ID (a long number, a long hex string, a UUID) are dropped.
- By hand: Cloudflare dashboard, then Analytics & Logs, then HTTP Traffic, filtered by path. Three columns are enough.

### search_console.csv: `date,engine,page,impressions,clicks,position`
- Google Search Console: verify readsharp.app with a DNS TXT record (no script). The export script uses the Search Console API (`searchanalytics.query`, dimensions `date` and `page`) with a service account added to the property as a restricted user. Set `engine` to `google`. Data lags about 2 days.
- Bing Webmaster Tools: verify through DNS or import the site from Search Console. Export Search Performance by page and add the rows with `engine` = `bing`. Bing matters for Windows users, because Edge and Windows search default to it.

### search_queries.csv: `date,engine,page,query,impressions,clicks,position` (optional)
The same Search Console data by query. It feeds the guide-topic suggestions only. Google leaves rare queries out of query rows, so totals always come from `search_console.csv`. The export also drops any query with an email or IP address, a link or an ID-like number, and any query with fewer than 10 impressions in the export window. Google withholds queries that might identify someone, but a name could still appear in a common query; check the file before sharing it.

### search_ads.csv: `date,platform,campaign,impressions,clicks,cost_usd`
- Google Ads: Reports, then a custom table by Day and Campaign with Impressions, Clicks and Cost. Schedule a weekly CSV email. `platform` = `google_ads`.
- Microsoft Advertising: Reports, then Campaign performance, daily, scheduled weekly. `platform` = `microsoft_ads`.
- Do not install either platform's conversion tag. Downloads come from the `/get/` links instead.

### stripe_events.csv: `event_id,date,type,plan,amount_usd,promo_code`
- `type` is one of `subscription_created`, `subscription_canceled` or `refund`. `plan` is `monthly` or `yearly`.
- Export script: lists subscriptions and refunds with the restricted key and rewrites the whole file each time. A subscription counts once paid (incomplete ones are skipped) under the plan it has now, so a reader who switched plans counts under the new one. A cancellation is dated the day the customer canceled, although access runs to the end of the paid period. A refund's plan comes from the charge it refunds. `event_id` only numbers each day's events (`2026-11-20-subscription_created-monthly-1`) and holds no Stripe ID. `promo_code` stays empty.
- By hand: export from the Stripe Dashboard and keep only these columns; never export emails.

### stripe_fees.csv: `date,currency,gross,fees` (optional)
Charged amounts and Stripe's fees per day, from balance transactions. With `ai_usage.csv`, it replaces the assumed gross margin with a measured one.

### supabase_signups.csv: `date,accounts_created`
The export script reads the `reporting.daily_accounts` view (migration `202611220001_readsharp_reporting_views.sql`) and fills days without sign-ups with 0. By hand, in the Supabase SQL editor (counts only):
```sql
select day as date, accounts_created from reporting.daily_accounts order by day;
```

### cohorts.csv: `cohort_week,accounts,paid_7d,paid_14d` (optional)
From `reporting.weekly_signup_cohorts`: accounts by sign-up week (Monday) and how many paid within 7 and 14 days, from the first paid invoice the billing webhook recorded. Used in launch mode. Deleted accounts drop out of every week.

### ai_usage.csv: `date,tokens,requests` (optional)
From `reporting.daily_ai_tokens`: ReadSharp AI tokens and requests per day across all readers. The report prices tokens with `ai_cost_usd_per_million_tokens` in settings.

### releases.csv: `date,version,platform` (optional)
`python scripts/marketing/releases.py` (also run by the export script). The date is the day the publish workflow put the version on the update feed, from GitHub; a git tag or the CHANGELOG heading is used when GitHub can't confirm it.

### creator_posts.csv: `post_date,creator,platform,post_url,views,fee_usd,slug,promo_code`
Keep a Google Sheet with one row per post (your own posts too, with fee 0). Update views when you pay the creator, then export it as CSV, or read it through a Google Sheets connector. `slug` is the `c-<creator>` part of the creator's `/get/` link; leave `promo_code` empty.

## Weekly routine
Every Monday, run the export script into `inbox/`, add `search_ads.csv` and `creator_posts.csv`, and ask for the weekly report.

## Privacy note for the website
Aggregate request counts from the host's logs contain no personal data, but visitors should be told. Proposed wording for the privacy policy is waiting for the owner's approval; do not edit the policy from this skill.
