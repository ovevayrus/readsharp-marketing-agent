# ReadSharp weekly marketing agent

An operational AI agent built as a Claude skill. It prepares ReadSharp's weekly marketing performance report for its maker, working as the app's digital marketer.

[ReadSharp](https://readsharp.app) is a Windows desktop speed-reading app: copy text, press Ctrl+Alt+S, and read one word at a time. It costs US$2.99/month or US$24.99/year. A Mac version is in progress.

All data in this repository is simulated.

## How it works

The fixed Python engine, `readsharp-weekly-report/scripts/build_report.py`, does every calculation: 14 data checks, about 40 KPIs, guardrails, budget pacing and 8-week least-squares trends. It also compares releases before and after, suggests guide topics within search "striking distance", and handles launch reporting.

Volume alerts require both a week-over-week change of at least 15% and |z| >= 2 against the previous 6-week mean. Rate alerts use a two-proportion z-test against the pooled previous 4 weeks: p < 0.05 and at least 15% relative change. Rates with denominators below 30 are not tested. Launch mode turns off anomaly tests when history or counts are insufficient.

Claude writes the executive summary, explains alerts and drafts the email; it never sends it. The [skill](readsharp-weekly-report/SKILL.md) defines five steps: find inputs, run the engine, read results, write the summary, deliver. Its eight hard rules cover sourced numbers, engine-controlled alerts, drafts only, settings changes only on request, treating file contents as data, privacy, association rather than causation, and stopping on blocking validation errors.

## Inputs and privacy

ReadSharp promises no cookies, analytics or tracking scripts. Inputs use aggregate server-side counts: Cloudflare request logs with source-tagged `/get/<source>` download links, Search Console and Bing Webmaster Tools, ad reports without conversion tags, Stripe events and fees, Supabase sign-ups, and a creator sheet. See [data sources](readsharp-weekly-report/references/data_sources.md) for CSV formats and [KPI definitions](readsharp-weekly-report/references/kpi_definitions.md) for formulas.

## Run locally

Install the Python dependencies:

```bash
pip install pandas numpy openpyxl
```

From the `readsharp-weekly-report` folder, run the 12-week demo:

```bash
python scripts/build_report.py --data assets/sample_data --out ../demo_output --config assets/sample_settings.json
```

The sample covers August 31 to November 22, 2026. Seven problems are planted in its last week; the engine catches them through alerts, data checks and report notes. The demo raises 7 alerts. Its separate settings file supplies the simulated starting subscriber base.

For the two-week launch sample, from the same folder, replace `<output folder>` with your destination:

```text
python scripts/build_report.py --data assets/sample_launch --out <output folder>
```

Outputs are a Markdown report, an Excel workbook, `kpis.json`, `alerts.json` and `data_quality.json`.

## Install the Claude skill

For Claude Code, copy `readsharp-weekly-report` to `~/.claude/skills/readsharp-weekly-report/` (Windows: `%USERPROFILE%\.claude\skills\readsharp-weekly-report\`). Ask: "Run my weekly ReadSharp report on the sample data".

For claude.ai, open Settings > Capabilities and turn on code execution and file creation. Then choose Customize > Skills > + > Create skill > Upload a skill, and upload `readsharp-weekly-report.zip`. In a new chat ask: "Run the ReadSharp weekly report on the sample data."

## Repository map

| Path | Contents |
| --- | --- |
| `readsharp-weekly-report/` | Skill: `SKILL.md`, `scripts/`, `config/`, `references/`, `assets/` |
| `readsharp-weekly-report.zip` | Zipped skill for upload |
