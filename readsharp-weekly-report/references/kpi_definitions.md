# KPI definitions and alert rules

All weeks run Monday to Sunday. "This week" is the report week; "baseline" is described per test below.

## Acquisition and funnel
| KPI | Formula | Source |
|---|---|---|
| Website page views | requests to `/`, `*.html`, clean page paths such as `/download`, `/lp/*`, `/c/*`; with a `status` column, only pages served (2xx or 304), so an `.html` request that redirects to its clean address counts once | Cloudflare |
| Download clicks | requests to `/get/<source>` (302 redirects to the installer) | Cloudflare |
| Raw installer requests | requests matching `installer_pattern` (`ReadSharp-Setup-*.exe`) | Cloudflare |
| Updater downloads (inferred) | requests for the newest version's `.blockmap`: every update fetches it (the old version's only when it isn't cached), a website install fetches none. Without blockmap rows: installer requests minus download clicks (floor 0). Excluded from downloads | Cloudflare |
| Update checks | requests for `latest.yml` (each running copy checks at start and every 6 hours) | Cloudflare |
| Sent-link opens | requests to `/e` ("Email me the link") and `/s` ("Send to my PC") | Cloudflare |
| Page views from Macs | page views whose `os` matches `mac_os_pattern` (only when the export has an `os` column) | Cloudflare |
| Accounts created | daily count of new accounts | Supabase |
| New paid subscribers | Stripe `subscription_created` events, de-duplicated by `event_id` | Stripe |
| Page view to download | download clicks / page views | derived |
| Download to account | accounts / download clicks | derived |
| Account to paid | new subscribers / accounts | derived |

## Channels
| KPI | Formula |
|---|---|
| Organic clicks, impressions | Search Console (Google) + Bing Webmaster Tools; when an engine's data ends before the week does (reporting lag), the week is scaled by 7 / days covered. Earlier days without rows had no impressions and are not scaled |
| Organic CTR | clicks / impressions |
| Avg. position | impression-weighted mean position |
| Ad CTR, CPC | clicks / impressions; cost / clicks |
| Ad click to download | `/get/google-ads` (or `/get/microsoft-ads`) clicks / ad clicks |
| Ad cost per download | ad cost / ad downloads (weekly and rolling 4-week) |
| Creator downloads | `/get/c-<creator>` clicks (ReadSharp's checkout takes no promotion codes) |
| Guide-topic suggestions | queries over the last 28 days with impression-weighted position 8 to 20 and at least 50 impressions (`striking_distance` in settings), most impressions first. Never alerts |

## Revenue and efficiency
| KPI | Formula |
|---|---|
| Active subscribers | starting base (settings) + created - canceled, per plan. A subscriber leaves the count on the day they cancel, though their paid access runs to the end of the period, so MRR is renewing revenue |
| MRR | monthly active x 2.99 + yearly active x 24.99 / 12 |
| Net new MRR | MRR this week - MRR last week |
| Blended CAC, 4-week | (ad cost + creator fees) over 4 weeks / new subscribers over 4 weeks |
| Monthly churn, 4-week | cancellations over 4 weeks / active subscribers 4 weeks ago x 30.44 / 28 |
| Refund rate, 4-week | refunds / new subscribers over 4 weeks |
| ARPU (monthly) | MRR / active subscribers |
| Gross margin | with `stripe_fees.csv` and `ai_usage.csv`: 1 - Stripe fees / charges - monthly AI cost / MRR, over the last 4 weeks (AI cost = tokens x `ai_cost_usd_per_million_tokens`); otherwise `gross_margin` in settings. The report says which |
| LTV | ARPU x gross margin x min(1 / monthly churn, 60 months) |
| LTV to CAC | LTV / blended CAC, 4-week |
| CAC payback (months) | blended CAC / (ARPU x gross margin) |

## Alert rules (from `config/settings.json`)
1. **Counts and costs (volume test).** Alert when the week-over-week change is at least 15% AND the value is at least 2 standard deviations from the mean of the previous 6 weeks (z-score). High severity at |z| >= 3. Only one condition met = "Watch".
2. **Rates (proportion test).** Two-sided two-proportion z-test of this week against the pooled previous 4 weeks. Alert when p < 0.05 AND the relative change is at least 15%. High severity at p < 0.001. A rate whose denominator is under 30 is marked "Low volume" and not tested; one whose numerator exceeds its denominator (more accounts than download clicks, say) is marked "Info" and not tested. 95% Wilson intervals are in the workbook.
3. **Guardrails (absolute limits).** 4-week cost per download above US$2.50, 4-week blended CAC above US$25, LTV to CAC below 3, 4-week refund rate above 3%, monthly churn above 8%. High severity when 50% past the limit.
4. **Budget pacing.** Month-to-date spend vs budget x days elapsed / days in month; alert beyond +/-10%.
5. **Grouping.** When a parent metric alerts (for example, organic clicks), children that alert in the same direction (Google clicks, Bing clicks, CTRs) are folded into the parent's note instead of getting separate rows.

## Launch mode
The report switches to launch mode when there are fewer than 3 full weeks of data (`min_full_weeks`), or when the report week has fewer than 30 page views, download clicks or accounts (`min_denominator`). Then:
- Volume and rate tests are off; those KPIs show "Not tested (launch mode)" with this week's value and 95% Wilson intervals for rates. A partial first week is kept as history.
- Guardrails on blended CAC, LTV to CAC, refund rate and churn, and the ad cost per download limits, are checked only once their 4-week count reaches 30. Budget pacing still applies.
- The report adds the funnel since the first download click with 95% Wilson intervals, daily counts for the last 14 days (`launch_daily_days`) and the sign-up cohorts from `cohorts.csv`. A cohort's 7-day share is final once its week ended 7 days before the report week's end (14 for the 14-day share); before that it is marked "still open".

## Releases
For each release in `releases.csv`, cancellations and refunds in the 7 days before the release day are set against the 7 days starting on it (never past the report week), with an exact two-sided binomial test of equal rates once both windows are complete. This is information, never an alert. A release inside the report week is noted on raw installer requests, updater downloads and update checks.

## Trends
Ordinary least-squares line over the last 8 weeks for organic clicks, download clicks, accounts, new subscribers and MRR, once there are at least 3 weeks. The report gives the slope per week, the slope as a percentage of the 8-week average, and R squared, which shows how steady the trend is.

## Why two conditions
ReadSharp's weekly counts are small, so a 15% swing is often noise. Requiring both a large change and a statistically unusual value keeps false alarms down; the guardrails catch slow drifts that never look unusual in a single week.
