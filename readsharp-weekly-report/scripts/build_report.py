#!/usr/bin/env python3
"""ReadSharp weekly marketing report engine (deterministic).

Reads the weekly CSV exports, validates them, computes every KPI with fixed
formulas, tests the latest week against its own history, and writes:

  report_<week_end>.md     report skeleton; Claude adds only the executive summary
  kpis_<week_end>.xlsx     Excel workbook: scorecard, history + chart, alerts,
                           channels, data quality, assumptions
  kpis.json, alerts.json, data_quality.json   machine-readable results for Claude

With fewer than 3 full weeks of history, or fewer than 30 page views, download clicks
or accounts in the report week, the report runs in LAUNCH MODE: anomaly tests are off
and it shows the cumulative funnel, daily counts and sign-up cohorts instead.

Same inputs + same settings -> the same numbers every week. No timestamps are
written, and each input file's SHA-256 hash is recorded for the audit trail.

Usage:
  python build_report.py --data <folder of CSVs> --out <output folder>
                         [--config <settings.json>] [--week-end YYYY-MM-DD]
                         [--validate-only]
Exit codes: 0 = report built, 2 = blocking data problem (see data_quality.json).
"""
import argparse
import hashlib
import json
import math
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "config" / "settings.json"
NAN = float("nan")

# ----------------------------------------------------------------------------- input contract
SCHEMAS = {
    "cloudflare_requests": {
        "file": "cloudflare_requests.csv", "required": True, "keys": ["date", "path"],
        "cols": {"date": "date", "path": "str", "requests": "int"},
        # Added by scripts/marketing/export_weekly.py; device type and OS only when the Cloudflare plan has them.
        "optional": {"host": "str", "status": "int", "device_type": "str?", "os": "str?"}},
    "search_console": {
        "file": "search_console.csv", "required": False, "keys": ["date", "engine", "page"],
        "cols": {"date": "date", "engine": "str", "page": "str", "impressions": "int", "clicks": "int",
                 "position": "float"}, "lower": ["engine"]},
    "search_ads": {
        "file": "search_ads.csv", "required": False, "keys": ["date", "platform", "campaign"],
        "cols": {"date": "date", "platform": "str", "campaign": "str", "impressions": "int", "clicks": "int",
                 "cost_usd": "float"}, "lower": ["platform"]},
    "stripe_events": {
        "file": "stripe_events.csv", "required": True, "keys": ["event_id"],
        "cols": {"event_id": "str", "date": "date", "type": "str", "plan": "str", "amount_usd": "float",
                 "promo_code": "str?"}, "lower": ["type", "plan"]},
    "supabase_signups": {
        "file": "supabase_signups.csv", "required": True, "keys": ["date"],
        "cols": {"date": "date", "accounts_created": "int"}},
    "creator_posts": {
        "file": "creator_posts.csv", "required": False, "keys": ["post_date", "creator", "post_url"],
        "cols": {"post_date": "date", "creator": "str", "platform": "str", "post_url": "str?", "views": "int",
                 "fee_usd": "float", "slug": "str", "promo_code": "str?"}},
    # Optional extras from scripts/marketing/export_weekly.py; a missing one is noted, not warned about.
    "search_queries": {
        "file": "search_queries.csv", "required": False, "missing": "INFO", "keys": ["date", "engine", "page", "query"],
        "cols": {"date": "date", "engine": "str", "page": "str", "query": "str", "impressions": "int", "clicks": "int",
                 "position": "float"}, "lower": ["engine"]},
    "releases": {
        "file": "releases.csv", "required": False, "missing": "INFO", "keys": ["date", "version", "platform"],
        "cols": {"date": "date", "version": "str", "platform": "str"}},
    "cohorts": {
        "file": "cohorts.csv", "required": False, "missing": "INFO", "keys": ["cohort_week"],
        "cols": {"cohort_week": "date", "accounts": "int", "paid_7d": "int", "paid_14d": "int"}},
    "stripe_fees": {
        "file": "stripe_fees.csv", "required": False, "missing": "INFO", "keys": ["date", "currency"],
        "cols": {"date": "date", "currency": "str", "gross": "float", "fees": "float"}, "lower": ["currency"]},
    "ai_usage": {
        "file": "ai_usage.csv", "required": False, "missing": "INFO", "keys": ["date"],
        "cols": {"date": "date", "tokens": "int", "requests": "int"}},
}
EVENT_TYPES = {"subscription_created": "new", "created": "new", "new": "new",
               "subscription_canceled": "canceled", "subscription_cancelled": "canceled",
               "canceled": "canceled", "cancelled": "canceled",
               "refund": "refund", "refunded": "refund", "charge_refunded": "refund"}
PLANS = {"monthly": "monthly", "month": "monthly", "yearly": "yearly", "annual": "yearly", "year": "yearly"}
ENGINES = ("google", "bing")
PLATFORMS = {"google_ads": "Google Ads", "microsoft_ads": "Microsoft Ads"}

# key, label, unit, polarity (+1 higher is better, -1 lower is better, 0 neutral), test, numerator, denominator, group
KPIS = [
    ("page_views", "Website page views", "count", 1, "volume", None, None, "Acquisition"),
    ("dl_total", "Download clicks (all sources)", "count", 1, "volume", None, None, "Acquisition"),
    ("installer_requests", "Raw installer requests (incl. auto-updates)", "count", 0, "none", None, None, "Acquisition"),
    ("updater_downloads", "Updater downloads (inferred)", "count", 0, "none", None, None, "Acquisition"),
    ("update_checks", "Update checks by installed copies", "count", 0, "none", None, None, "Acquisition"),
    ("email_link_opens", "\"Email me the link\" links opened", "count", 1, "none", None, None, "Acquisition"),
    ("share_link_opens", "\"Send to my PC\" links opened", "count", 1, "none", None, None, "Acquisition"),
    ("mac_page_views", "Website page views from Macs", "count", 0, "none", None, None, "Acquisition"),
    ("visit_to_download", "Page view to download", "pct", 1, "rate", "dl_total", "page_views", "Funnel"),
    ("accounts", "Accounts created", "count", 1, "volume", None, None, "Funnel"),
    ("download_to_account", "Download to account", "pct", 1, "rate", "accounts", "dl_total", "Funnel"),
    ("new_subs", "New paid subscribers", "count", 1, "volume", None, None, "Funnel"),
    ("account_to_paid", "Account to paid", "pct", 1, "rate", "new_subs", "accounts", "Funnel"),
    ("organic_clicks", "Organic search clicks (Google + Bing)", "count", 1, "volume", None, None, "Organic search"),
    ("google_clicks", "Google organic clicks", "count", 1, "volume", None, None, "Organic search"),
    ("google_ctr", "Google organic CTR", "pct", 1, "rate", "google_clicks", "google_impressions", "Organic search"),
    ("google_position", "Google avg. position", "pos", -1, "volume", None, None, "Organic search"),
    ("bing_clicks", "Bing organic clicks", "count", 1, "volume", None, None, "Organic search"),
    ("bing_ctr", "Bing organic CTR", "pct", 1, "rate", "bing_clicks", "bing_impressions", "Organic search"),
    ("google_ads_cost", "Google Ads spend", "usd", 0, "volume", None, None, "Search ads"),
    ("google_ads_ctr", "Google Ads CTR", "pct", 1, "rate", "google_ads_clicks", "google_ads_impressions", "Search ads"),
    ("google_ads_cpc", "Google Ads cost per click", "usd", -1, "volume", None, None, "Search ads"),
    ("google_ads_click_to_download", "Google Ads click to download", "pct", 1, "rate", "google_ads_downloads",
     "google_ads_clicks", "Search ads"),
    ("google_ads_cpd", "Google Ads cost per download", "usd", -1, "volume", None, None, "Search ads"),
    ("google_ads_cpd_4w", "Google Ads cost per download, 4-week", "usd", -1, "none", None, None, "Search ads"),
    ("microsoft_ads_cost", "Microsoft Ads spend", "usd", 0, "volume", None, None, "Search ads"),
    ("microsoft_ads_ctr", "Microsoft Ads CTR", "pct", 1, "rate", "microsoft_ads_clicks", "microsoft_ads_impressions",
     "Search ads"),
    ("microsoft_ads_cpc", "Microsoft Ads cost per click", "usd", -1, "volume", None, None, "Search ads"),
    ("microsoft_ads_click_to_download", "Microsoft Ads click to download", "pct", 1, "rate", "microsoft_ads_downloads",
     "microsoft_ads_clicks", "Search ads"),
    ("microsoft_ads_cpd", "Microsoft Ads cost per download", "usd", -1, "volume", None, None, "Search ads"),
    ("microsoft_ads_cpd_4w", "Microsoft Ads cost per download, 4-week", "usd", -1, "none", None, None, "Search ads"),
    ("dl_creators", "Creator and own-post downloads", "count", 1, "volume", None, None, "Creators"),
    ("yearly_share", "Yearly plan share of new subs", "pct", 0, "rate", "new_yearly", "new_subs", "Revenue"),
    ("cancels", "Cancellations", "count", -1, "volume", None, None, "Revenue"),
    ("active_subs", "Active subscribers (end of week)", "count", 1, "none", None, None, "Revenue"),
    ("mrr", "Monthly recurring revenue (MRR)", "usd", 1, "none", None, None, "Revenue"),
    ("net_new_mrr", "Net new MRR", "usd", 1, "volume", None, None, "Revenue"),
    ("spend_total", "Marketing spend (ads + creator fees)", "usd", 0, "none", None, None, "Efficiency"),
    ("blended_cac_4w", "Blended CAC, 4-week", "usd", -1, "volume", None, None, "Efficiency"),
    ("churn_monthly_pct", "Monthly churn, 4-week", "pctpts", -1, "none", None, None, "Efficiency"),
    ("refund_rate_4w_pct", "Refund rate, 4-week", "pctpts", -1, "none", None, None, "Efficiency"),
    ("ltv_usd", "Customer lifetime value (LTV)", "usd", 1, "none", None, None, "Efficiency"),
    ("ltv_to_cac", "LTV to CAC", "x", 1, "none", None, None, "Efficiency"),
    ("payback_months", "CAC payback", "months", -1, "none", None, None, "Efficiency"),
]
TREND_KEYS = ["organic_clicks", "dl_total", "accounts", "new_subs", "mrr"]
RELATED = {  # parent KPI: children folded into the parent's alert when they move the same way
    "organic_clicks": ["google_clicks", "bing_clicks", "google_ctr", "bing_ctr", "google_position"],
    "microsoft_ads_cpc": ["microsoft_ads_cpd", "microsoft_ads_ctr", "microsoft_ads_click_to_download"],
    "google_ads_cpc": ["google_ads_cpd", "google_ads_ctr", "google_ads_click_to_download"],
}


class DataQuality:
    def __init__(self):
        self.items = []

    def add(self, check, severity, source, message, rows=0):
        self.items.append({"check": check, "severity": severity, "source": source, "message": message,
                           "rows": int(rows)})

    def blocking(self):
        return [i for i in self.items if i["severity"] == "ERROR" and i.get("blocking")]

    def block(self, check, source, message):
        self.items.append({"check": check, "severity": "ERROR", "source": source, "message": message,
                           "rows": 0, "blocking": True})


# ----------------------------------------------------------------------------- loading + validation
def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_source(data_dir, spec, dq):
    path = data_dir / spec["file"]
    if not path.exists():
        if spec["required"]:
            dq.block("V01 file present", spec["file"], "Required file not found; the report cannot be built.")
        else:
            dq.add("V01 file present", spec.get("missing", "WARN"), spec["file"], "File not found; its report section is skipped.")
        return None
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    raw.columns = [c.strip().lower() for c in raw.columns]
    extra = [c for c in spec.get("optional", {}) if c in raw.columns]
    cols = {**spec["cols"], **{c: spec["optional"][c] for c in extra}}
    keys = spec["keys"] + extra
    missing = [c for c in spec["cols"] if c not in raw.columns]
    if missing:
        msg = f"Missing column(s): {', '.join(missing)}."
        if spec["required"]:
            dq.block("V02 required columns", spec["file"], msg + " The report cannot be built.")
        else:
            dq.add("V02 required columns", "ERROR", spec["file"], msg + " Section skipped.")
        return None
    df = raw[list(cols)].copy()
    bad = pd.Series(False, index=df.index)
    for col, kind in cols.items():
        optional = kind.endswith("?")
        kind = kind.rstrip("?")
        s = df[col].astype(str).str.strip()
        if kind == "date":
            parsed = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
            df[col] = parsed
            bad |= parsed.isna()
        elif kind in ("int", "float"):
            parsed = pd.to_numeric(s.str.replace(",", "", regex=False).str.replace("$", "", regex=False),
                                   errors="coerce")
            df[col] = parsed
            bad |= parsed.isna()
        else:
            df[col] = s
            if not optional:
                bad |= s.eq("")
    if bad.any():
        dq.add("V03 readable values", "ERROR", spec["file"],
               f"{int(bad.sum())} row(s) with a blank or unreadable value were excluded.", bad.sum())
        df = df[~bad]
    num_cols = [c for c, k in cols.items() if k.rstrip("?") in ("int", "float")]
    if num_cols and len(df):
        neg = (df[num_cols] < 0).any(axis=1)
        if neg.any():
            dq.add("V05 no negative values", "ERROR", spec["file"],
                   f"{int(neg.sum())} row(s) with negative numbers were excluded.", neg.sum())
            df = df[~neg]
    for col, kind in cols.items():
        if kind == "int":
            df[col] = df[col].round().astype("int64")
    for col in spec.get("lower", []):
        df[col] = df[col].str.lower()
    dupes = df.duplicated(subset=keys, keep="first")
    if dupes.any():
        dq.add("V04 no duplicates", "WARN", spec["file"],
               f"{int(dupes.sum())} duplicate row(s) on ({', '.join(keys)}) removed; kept the first.",
               dupes.sum())
        df = df[~dupes]
    if {"impressions", "clicks"} <= set(df.columns):
        impossible = df["clicks"] > df["impressions"]
        if impossible.any():
            dq.add("V06 clicks <= impressions", "ERROR", spec["file"],
                   f"{int(impossible.sum())} row(s) with more clicks than impressions were excluded.",
                   impossible.sum())
            df = df[~impossible]
    return df.reset_index(drop=True)


def week_of(dates):
    return dates - pd.to_timedelta(dates.dt.weekday, unit="D")


def weekly_sum(df, datecol, valcol, weeks, mask=None):
    d = df if mask is None else df[mask]
    if d is None or not len(d):
        return pd.Series(0.0, index=weeks)
    return d.groupby(week_of(d[datecol]))[valcol].sum().reindex(weeks, fill_value=0).astype(float)


def weekly_days(df, datecol, weeks, mask=None):
    d = df if mask is None else df[mask]
    if d is None or not len(d):
        return pd.Series(0, index=weeks)
    return d.groupby(week_of(d[datecol]))[datecol].nunique().reindex(weeks, fill_value=0)


def div(a, b):
    """Elementwise a / b with NaN where b == 0."""
    if isinstance(b, pd.Series):
        return a / b.where(b != 0)
    return a / b if b else NAN


# ----------------------------------------------------------------------------- statistics
def two_prop_test(x1, n1, x2, n2):
    """Two-sided two-proportion z-test. Returns (z, p_value)."""
    pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (x1 / n1 - x2 / n2) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


def wilson(x, n, z=1.96):
    """95% Wilson score interval for a proportion."""
    if n <= 0 or not 0 <= x <= n:
        return NAN, NAN
    p = x / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def binom_two_sided(a, b):
    """Exact two-sided p-value that counts a and b come from one shared rate (binomial, p = 0.5)."""
    n = a + b
    if n == 0:
        return NAN
    cdf = lambda k: sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * min(cdf(a), 1 - cdf(a - 1)))


def num(v):
    """JSON-safe number."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if math.isnan(f) or math.isinf(f) else round(f, 6)


# ----------------------------------------------------------------------------- formatting
def fmt(v, unit):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if unit == "count":
        return f"{v:,.0f}"
    if unit == "usd":
        return f"${v:,.2f}" if abs(v) < 1000 else f"${v:,.0f}"
    if unit == "pct":
        return f"{v * 100:.1f}%"
    if unit == "pctpts":
        return f"{v:.1f}%"
    if unit == "pos":
        return f"{v:.1f}"
    if unit == "x":
        return f"{v:.1f}x"
    if unit == "months":
        return f"{v:.1f} mo"
    return str(v)


def fmt_change(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v * 100:+.0f}%"


STATUS_TEXT = {"alert_bad": "ALERT (worse)", "alert_good": "ALERT (better)", "alert_neutral": "ALERT",
               "watch": "Watch", "ok": "OK", "low_volume": "Low volume", "info": "Info",
               "not_tested": "Not tested (launch mode)"}


# ----------------------------------------------------------------------------- main computation
def build(data_dir, cfg, week_end_arg, dq):
    data, inputs = {}, {}
    for name, spec in SCHEMAS.items():
        data[name] = load_source(data_dir, spec, dq)
        p = data_dir / spec["file"]
        if p.exists():
            inputs[spec["file"]] = {"sha256": sha256(p), "rows_used": 0 if data[name] is None else len(data[name])}
    if dq.blocking():
        return None

    st = data["stripe_events"]
    st["kind"] = st["type"].map(EVENT_TYPES)
    unknown = st["kind"].isna()
    if unknown.any():
        dq.add("V07 known Stripe event types", "WARN", "stripe_events.csv",
               f"{int(unknown.sum())} event(s) of unknown type ({', '.join(sorted(st.loc[unknown, 'type'].unique()))}) "
               "were ignored.", unknown.sum())
    st["plan"] = st["plan"].map(PLANS)
    unknown_plan = st["plan"].isna() & ~unknown
    if unknown_plan.any():
        dq.add("V07 known Stripe plans", "WARN", "stripe_events.csv",
               f"{int(unknown_plan.sum())} event(s) with an unknown plan were ignored.", unknown_plan.sum())
    st = st[st["kind"].notna() & st["plan"].notna()].copy()

    cf = data["cloudflare_requests"]
    last_day = cf["date"].max()
    if week_end_arg:
        week_end = pd.Timestamp(week_end_arg)
        if week_end.weekday() != 6:
            dq.block("V12 report week", "--week-end", f"{week_end.date()} is not a Sunday; weeks run Monday to Sunday.")
            return None
        if week_end > last_day:
            dq.block("V12 report week", "cloudflare_requests.csv",
                     f"Data ends {last_day.date()}, before the requested week end {week_end.date()}.")
            return None
    else:
        week_end = last_day - pd.Timedelta(days=(last_day.weekday() + 1) % 7)
    week_start = week_end - pd.Timedelta(days=6)
    th, gr, P = cfg["thresholds"], cfg["guardrails"], cfg["paths"]
    first_day = cf["date"].min()
    first_monday = first_day - pd.Timedelta(days=first_day.weekday())
    first_ws = first_monday + pd.Timedelta(days=7 if first_day.weekday() else 0)  # skip a partial first week
    weeks = pd.date_range(first_ws, week_start, freq="7D")
    launch_reasons = []
    if len(weeks) < th["min_full_weeks"]:
        launch_reasons.append(f"there {'is' if len(weeks) == 1 else 'are'} only {len(weeks)} full week{'' if len(weeks) == 1 else 's'} "
                              f"of data (week-over-week tests need {th['min_full_weeks']})")
        weeks = pd.date_range(first_monday, week_start, freq="7D")  # keep the partial first week
    if not len(weeks):
        dq.block("V12 report week", "cloudflare_requests.csv",
                 f"Data starts {first_day.date()}, after the report week ending {week_end.date()}; "
                 "rerun once a Monday-to-Sunday week has ended.")
        return None
    t = len(weeks) - 1
    W = pd.DataFrame(index=weeks)
    notes = {}

    # --- Cloudflare: pages, source-tagged download clicks, installer, update checks. With the exporter's
    # host and status columns, pages count on the site host when served (2xx or 304), so an .html
    # request that redirects to its clean address isn't counted twice.
    path = cf["path"]
    on_site = cf["host"].eq(P["site_host"]) if "host" in cf else pd.Series(True, index=cf.index)
    on_downloads = cf["host"].eq(P["download_host"]) if "host" in cf else pd.Series(True, index=cf.index)
    served = (cf["status"].between(200, 299) | cf["status"].eq(304)) if "status" in cf else pd.Series(True, index=cf.index)
    is_get = path.str.startswith(P["download_click_prefix"]) & on_site
    short_links = [P["email_link"], P["share_link"]]
    clean_page = path.str.fullmatch(r"/[a-z0-9-]+(/[a-z0-9-]+)?") & ~path.isin(short_links) & ~path.str.startswith(P["download_click_prefix"])
    is_page = (path.eq("/") | path.str.endswith(".html") | path.str.startswith(tuple(P["landing_prefixes"])) | clean_page) \
        & on_site & served & ~path.str.startswith(P["download_click_prefix"])
    cf["source"] = np.where(is_get, path.str.slice(len(P["download_click_prefix"])), "")
    creators = data["creator_posts"]
    creator_slugs = set(creators["slug"]) if creators is not None else set()

    def group_of(src):
        for key, label in cfg["download_sources"].items():
            if key.endswith("-"):
                if src.startswith(key):
                    return label if (not creator_slugs or src in creator_slugs) else None
            elif src == key:
                return label
        return None

    cf["group"] = [group_of(s) if g else None for s, g in zip(cf["source"], is_get)]
    unregistered = is_get & cf["group"].isna()
    W["page_views"] = weekly_sum(cf, "date", "requests", weeks, is_page)
    W["dl_total"] = weekly_sum(cf, "date", "requests", weeks, is_get)
    W["dl_site"] = weekly_sum(cf, "date", "requests", weeks, is_get & cf["source"].eq("site"))
    W["google_ads_downloads"] = weekly_sum(cf, "date", "requests", weeks, is_get & cf["source"].eq("google-ads"))
    W["microsoft_ads_downloads"] = weekly_sum(cf, "date", "requests", weeks, is_get & cf["source"].eq("microsoft-ads"))
    W["dl_creators"] = weekly_sum(cf, "date", "requests", weeks, is_get & cf["group"].eq(cfg["download_sources"]["c-"]))
    W["dl_unregistered"] = weekly_sum(cf, "date", "requests", weeks, unregistered)
    matches = lambda pattern: path.map(lambda p: fnmatchcase(p, pattern)).astype(bool) & on_downloads
    is_installer, is_blockmap = matches(P["installer_pattern"]), matches(P["update_blockmap_pattern"])
    is_update_check = path.eq(P["update_check"]) & on_downloads
    W["installer_requests"] = weekly_sum(cf, "date", "requests", weeks, is_installer)
    W["update_checks"] = weekly_sum(cf, "date", "requests", weeks, is_update_check)
    # Every update downloads the new version's .blockmap (the old one only when it isn't cached),
    # and an install from the website fetches none, so updates are the requests for the newest
    # version's blockmap seen so far. Without blockmap rows, fall back to installer requests that
    # didn't follow a download click.
    parts = path.str.extract(r"-(\d+)\.(\d+)\.(\d+)-").astype(float)
    vnum = parts[0] * 1e6 + parts[1] * 1e3 + parts[2]
    newest = vnum[is_blockmap].groupby(cf.loc[is_blockmap, "date"]).max().sort_index().cummax()
    is_new_blockmap = is_blockmap & vnum.eq(cf["date"].map(newest))
    updater_method = "blockmap" if is_blockmap.any() else "residual"
    if updater_method == "blockmap":
        W["updater_downloads"] = weekly_sum(cf, "date", "requests", weeks, is_new_blockmap)
    else:
        W["updater_downloads"] = (W["installer_requests"] - W["dl_total"]).clip(lower=0)
    for key, link in (("email_link_opens", P["email_link"]), ("share_link_opens", P["share_link"])):
        if (path.eq(link) & on_site).any():
            W[key] = weekly_sum(cf, "date", "requests", weeks, path.eq(link) & on_site)
    if "os" in cf:
        W["mac_page_views"] = weekly_sum(cf, "date", "requests", weeks,
                                         is_page & cf["os"].str.contains(cfg["mac_os_pattern"], case=False, regex=True))
    cf_days = weekly_days(cf, "date", weeks).iat[t]
    if cf_days < 7:
        dq.add("V08 complete week", "WARN", "cloudflare_requests.csv",
               f"Only {cf_days} of 7 days present for the report week.")
    in_week = (cf["date"] >= week_start) & (cf["date"] <= week_end)
    if "status" in cf and (is_get & in_week & cf["status"].ne(302)).any():
        bad_get = cf[is_get & in_week & cf["status"].ne(302)].groupby("status")["requests"].sum()
        dq.add("V15 download links redirect", "WARN", "cloudflare_requests.csv",
               "Some /get/ requests were not redirected (HTTP " + ", ".join(f"{k}: {v:,}" for k, v in bad_get.items())
               + "). Check that the deployed site has its _redirects file.", bad_get.sum())
    if (unregistered & in_week).any():
        u = cf[unregistered & in_week].groupby("source")["requests"].sum()
        dq.add("V09 registered download links", "WARN", "cloudflare_requests.csv",
               "Download clicks from link(s) not listed in settings or creator_posts: "
               + ", ".join(f"/get/{k} ({v:,})" for k, v in u.items())
               + ". Counted in total downloads but not in any channel.", u.sum())
    if W["installer_requests"].iat[t] < W["dl_total"].iat[t]:
        dq.add("V10 installer >= download clicks", "WARN", "cloudflare_requests.csv",
               "Fewer installer requests than download clicks: a redirect or logging rule may be broken.")

    # --- Search Console + Bing Webmaster Tools
    sc = data["search_console"]
    if sc is not None:
        for e in ENGINES:
            m = sc["engine"].eq(e)
            # Only the days after an engine's latest data count as reporting lag and are scaled for.
            # An earlier day without rows simply had no impressions (common at launch), so it isn't.
            last = sc.loc[m, "date"].max() if m.any() else None
            days = pd.Series([7 - min(7, max(0, (ws + pd.Timedelta(days=6) - last).days)) if last is not None else 0
                              for ws in weeks], index=weeks)
            scale = (7 / days.where(days > 0)).where(days < 7, 1.0).fillna(1.0)
            W[f"{e}_days"] = days
            W[f"{e}_impressions"] = weekly_sum(sc, "date", "impressions", weeks, m) * scale
            W[f"{e}_clicks"] = weekly_sum(sc, "date", "clicks", weeks, m) * scale
            wpos = sc.loc[m, "position"] * sc.loc[m, "impressions"]
            W[f"{e}_position"] = div(weekly_sum(sc.assign(wpos=wpos), "date", "wpos", weeks, m),
                                     weekly_sum(sc, "date", "impressions", weeks, m))
            W[f"{e}_ctr"] = div(W[f"{e}_clicks"], W[f"{e}_impressions"])
            if 0 < days.iat[t] < 7:
                missing = list(pd.date_range(last + pd.Timedelta(days=1), week_end))
                notes[e] = (f"{e.title()} reported {days.iat[t]} of 7 days (missing "
                            f"{', '.join(d.strftime('%b %d') for d in missing)}); weekly totals are scaled to 7 days.")
                dq.add("V11 search data complete", "INFO", "search_console.csv",
                       notes[e] + " Search Console normally lags about 2 days; rerun mid-week for final figures.")
        W["organic_clicks"] = W["google_clicks"] + W["bing_clicks"]
        W["organic_impressions"] = W["google_impressions"] + W["bing_impressions"]

    # --- Search ads
    ads = data["search_ads"]
    if ads is not None:
        for p in PLATFORMS:
            m = ads["platform"].eq(p)
            for col, src in (("impressions", "impressions"), ("clicks", "clicks"), ("cost", "cost_usd")):
                W[f"{p}_{col}"] = weekly_sum(ads, "date", src, weeks, m)
            W[f"{p}_ctr"] = div(W[f"{p}_clicks"], W[f"{p}_impressions"])
            W[f"{p}_cpc"] = div(W[f"{p}_cost"], W[f"{p}_clicks"])
            W[f"{p}_click_to_download"] = div(W[f"{p}_downloads"], W[f"{p}_clicks"])
            W[f"{p}_cpd"] = div(W[f"{p}_cost"], W[f"{p}_downloads"])
            W[f"{p}_cpd_4w"] = div(W[f"{p}_cost"].rolling(4, min_periods=4).sum(),
                                   W[f"{p}_downloads"].rolling(4, min_periods=4).sum())
            if weekly_days(ads, "date", weeks, m).iat[t] < 7:
                dq.add("V08 complete week", "WARN", "search_ads.csv", f"{PLATFORMS[p]} is missing days in the report week.")
    else:
        for p in PLATFORMS:
            W[f"{p}_cost"] = 0.0

    # --- Supabase sign-ups
    su = data["supabase_signups"]
    W["accounts"] = weekly_sum(su, "date", "accounts_created", weeks)
    if weekly_days(su, "date", weeks).iat[t] < 7:
        dq.add("V08 complete week", "WARN", "supabase_signups.csv", "Missing days in the report week.")

    # --- Stripe subscriptions
    for col, mask in (("new_monthly", st["kind"].eq("new") & st["plan"].eq("monthly")),
                      ("new_yearly", st["kind"].eq("new") & st["plan"].eq("yearly")),
                      ("cancels", st["kind"].eq("canceled")),
                      ("refunds_n", st["kind"].eq("refund"))):
        W[col] = weekly_sum(st.assign(one=1), "date", "one", weeks, mask)
    W["refunds_usd"] = weekly_sum(st, "date", "amount_usd", weeks, st["kind"].eq("refund"))
    W["new_subs"] = W["new_monthly"] + W["new_yearly"]
    base = cfg["starting_active_subscribers"]
    as_of = pd.Timestamp(base["as_of"])
    week_ends = weeks + pd.Timedelta(days=6)
    prices = cfg["prices_usd"]
    for plan in ("monthly", "yearly"):
        sp = st[(st["plan"] == plan) & (st["date"] >= as_of)]
        delta = sp["kind"].map({"new": 1, "canceled": -1}).fillna(0)
        W[f"active_{plan}"] = [base[plan] + delta[sp["date"] <= we].sum() for we in week_ends]
    W["active_subs"] = W["active_monthly"] + W["active_yearly"]
    W["mrr"] = W["active_monthly"] * prices["monthly"] + W["active_yearly"] * prices["yearly"] / 12
    start_mrr = base["monthly"] * prices["monthly"] + base["yearly"] * prices["yearly"] / 12
    W["net_new_mrr"] = W["mrr"].diff()
    W.iloc[0, W.columns.get_loc("net_new_mrr")] = W["mrr"].iat[0] - start_mrr
    W["yearly_share"] = div(W["new_yearly"], W["new_subs"])
    if W["new_subs"].iat[t] > W["accounts"].iat[t]:
        dq.add("V13 subscribers <= accounts", "WARN", "stripe_events.csv",
               "More new subscribers than new accounts this week; check for duplicate Stripe events or a missing sign-up export.")

    # --- Funnel
    W["visit_to_download"] = div(W["dl_total"], W["page_views"])
    W["download_to_account"] = div(W["accounts"], W["dl_total"])
    W["account_to_paid"] = div(W["new_subs"], W["accounts"])
    low = [f"{label} ({W[key].iat[t]:,.0f})" for key, label in (("page_views", "page views"), ("dl_total", "download clicks"),
                                                               ("accounts", "accounts")) if W[key].iat[t] < th["min_denominator"]]
    if low:
        launch_reasons.append(f"the report week has fewer than {th['min_denominator']} {', '.join(low)}")
    launch = bool(launch_reasons)

    # --- Creators
    promo_map = {}
    if creators is not None:
        W["creator_fees"] = weekly_sum(creators, "post_date", "fee_usd", weeks)
        W["creator_views"] = weekly_sum(creators, "post_date", "views", weeks)
        promo_map = {r.promo_code.upper(): r.creator for r in creators.itertuples() if r.promo_code}
    else:
        W["creator_fees"] = 0.0
    promo = st["promo_code"].fillna("").str.upper()
    unknown_codes = sorted(set(promo[st["kind"].eq("new") & promo.ne("")]) - set(promo_map))
    if unknown_codes:
        dq.add("V14 known promo codes", "WARN", "stripe_events.csv",
               f"Promo code(s) not linked to a creator: {', '.join(unknown_codes)}.")
    W["promo_subs"] = weekly_sum(st.assign(one=1), "date", "one", weeks,
                                 st["kind"].eq("new") & promo.isin(list(promo_map)))

    # --- Spend, CAC, churn, unit economics
    W["spend_ads"] = W["google_ads_cost"] + W["microsoft_ads_cost"]
    W["spend_total"] = W["spend_ads"] + W["creator_fees"]
    W["blended_cac"] = div(W["spend_total"], W["new_subs"])
    roll = lambda c: W[c].rolling(4, min_periods=4).sum()
    W["spend_4w"], W["new_4w"], W["cancels_4w"], W["refunds_4w"] = (roll("spend_total"), roll("new_subs"),
                                                                     roll("cancels"), roll("refunds_n"))
    W["blended_cac_4w"] = div(W["spend_4w"], W["new_4w"])
    active_start = W["active_subs"].shift(4)
    W["churn_monthly_pct"] = div(W["cancels_4w"], active_start) * (30.44 / 28) * 100
    W["refund_rate_4w_pct"] = div(W["refunds_4w"], W["new_4w"]) * 100
    W["arpu_month"] = div(W["mrr"], W["active_subs"])
    # Gross margin: measured from Stripe's fees and ReadSharp AI's token cost over the last 4 weeks
    # when both files are present, otherwise the assumption in settings.json.
    lo4 = max(weeks[max(0, t - 3)], first_day)
    span_days = (week_end - lo4).days + 1
    fees, ai = data["stripe_fees"], data["ai_usage"]
    fee_rate = ai_month = NAN
    if fees is not None:
        f = fees[fees["date"].between(lo4, week_end)]
        currencies = sorted(set(f["currency"]))
        if len(currencies) > 1:  # amounts in different currencies can't be added up
            keep = cfg["currency"].lower() if cfg["currency"].lower() in currencies else f.groupby("currency")["gross"].sum().idxmax()
            dq.add("V17 one fee currency", "INFO", "stripe_fees.csv",
                   f"Charges in {', '.join(currencies)}; the fee rate uses the {keep} rows only.")
            f = f[f["currency"].eq(keep)]
        fee_rate = div(f["fees"].sum(), f["gross"].sum())
    if ai is not None:
        ai_month = ai.loc[ai["date"].between(lo4, week_end), "tokens"].sum() / 1e6 \
            * cfg["ai_cost_usd_per_million_tokens"] * 30.44 / span_days
    mrr_now = W["mrr"].iat[t]
    margin = cfg["gross_margin"]
    margin_info = {"value": margin, "measured": False, "fee_rate": fee_rate, "ai_cost_month_usd": ai_month,
                   "ai_share_of_mrr": div(ai_month, mrr_now) if mrr_now > 0 else NAN, "days": span_days,
                   "basis": "assumed: gross_margin in settings.json"}
    if fee_rate == fee_rate and ai_month == ai_month and mrr_now > 0:
        margin = min(max(1 - fee_rate - ai_month / mrr_now, 0.0), 1.0)
        margin_info.update(value=margin, measured=True,
                           basis=f"measured over {span_days} days: Stripe fees {fee_rate:.1%} of charges, "
                                 f"ReadSharp AI {ai_month / mrr_now:.1%} of MRR at the "
                                 f"${cfg['ai_cost_usd_per_million_tokens']:.2f} per million tokens set in settings.json")
    else:
        lacking = [name for name, v in (("stripe_fees.csv", fee_rate), ("ai_usage.csv", ai_month)) if v != v]
        if lacking or mrr_now <= 0:
            margin_info["basis"] += " (" + (f"no usable {' or '.join(lacking)}" if lacking else "MRR is 0") + ")"
    lifetime = (100 / W["churn_monthly_pct"].where(W["churn_monthly_pct"] > 0)).clip(upper=60)
    W["ltv_usd"] = W["arpu_month"] * margin * lifetime
    W["ltv_to_cac"] = div(W["ltv_usd"], W["blended_cac_4w"])
    W["payback_months"] = div(W["blended_cac_4w"], W["arpu_month"] * margin)

    # --- Releases: label update traffic, and compare cancellations and refunds in the 7 days
    # before and after each release (an association, shown as information, never an alert).
    releases = []
    rel = data["releases"]
    if rel is not None:
        for (day, version), rows in rel[rel["date"] <= week_end].groupby(["date", "version"], sort=True):
            before = st["date"].between(day - pd.Timedelta(days=7), day - pd.Timedelta(days=1))
            after = st["date"].between(day, min(day + pd.Timedelta(days=6), week_end))  # nothing after the report week
            row = {"date": f"{day:%Y-%m-%d}", "version": version, "platforms": ", ".join(sorted(set(rows["platform"]))),
                   "before_complete": bool(day - pd.Timedelta(days=7) >= first_day),
                   "after_complete": bool(day + pd.Timedelta(days=6) <= week_end)}
            for kind, name in (("canceled", "cancels"), ("refund", "refunds")):
                b, a = int((before & st["kind"].eq(kind)).sum()), int((after & st["kind"].eq(kind)).sum())
                # Equal windows are needed for the test, so it waits until both are complete.
                p = binom_two_sided(a, b) if row["before_complete"] and row["after_complete"] else NAN
                row.update({f"{name}_before": b, f"{name}_after": a, f"{name}_p": p})
            releases.append(row)
    week_releases = [r for r in releases if week_start <= pd.Timestamp(r["date"]) <= week_end]
    release_note = ("Released this week (releases.csv): " + "; ".join(
        f"{r['version']} on {pd.Timestamp(r['date']):%b %d}" for r in week_releases)
        + ". Installed copies download each release through the updater.") if week_releases else ""

    # --- Evaluate every KPI for the report week
    results = []
    for key, label, unit, pol, test, numer, denom, group in KPIS:
        if key not in W:
            continue
        s = W[key]
        cur, prev = s.iat[t], (s.iat[t - 1] if t > 0 else NAN)
        r = {"key": key, "label": label, "unit": unit, "group": group, "test": test, "current": cur,
             "previous": prev, "wow_pct": div(cur - prev, abs(prev)) if prev == prev else NAN,
             "baseline": NAN, "z": NAN, "p_value": NAN, "ci_low": NAN, "ci_high": NAN,
             "status": "ok", "severity": None, "direction": None, "note": ""}
        for e, note in notes.items():
            if key.startswith(e):
                r["note"] = note
        if release_note and key in ("installer_requests", "updater_downloads", "update_checks"):
            r["note"] = release_note
        ref = prev
        if launch and test in ("volume", "rate"):
            r["status"] = "not_tested"
            if test == "rate" and W[denom].iat[t] > 0:
                r["ci_low"], r["ci_high"] = wilson(W[numer].iat[t], W[denom].iat[t])
            results.append(r)
            continue
        if test == "volume":
            hist = s.iloc[max(0, t - th["baseline_weeks"]):t].dropna()
            if len(hist) >= 3:
                r["baseline"] = hist.mean()
                sd = hist.std(ddof=1)
                r["z"] = (cur - hist.mean()) / sd if sd > 0 else NAN
                ref = hist.mean()
            big = abs(r["wow_pct"]) >= th["wow_change_pct"] / 100 if r["wow_pct"] == r["wow_pct"] else False
            unusual = abs(r["z"]) >= th["z_score"] if r["z"] == r["z"] else False
            if big and unusual:
                r["status"] = "alert"
                r["severity"] = "high" if abs(r["z"]) >= th["z_score_high"] else "medium"
            elif big or unusual:
                r["status"] = "watch"
                r["note"] = (r["note"] + " " if r["note"] else "") + (
                    "Large week-over-week move but within normal range." if big else
                    "Unusual versus the 6-week baseline, but under the 15% change threshold.")
        elif test == "rate":
            x1, n1 = W[numer].iat[t], W[denom].iat[t]
            lo = max(0, t - th["rate_baseline_weeks"])
            x2, n2 = W[numer].iloc[lo:t].sum(), W[denom].iloc[lo:t].sum()
            if not n1 or n1 < th["min_denominator"]:
                r["status"] = "low_volume"
                r["note"] = (r["note"] + " " if r["note"] else "") + \
                    f"Only {n1:,.0f} in the denominator (minimum {th['min_denominator']}); not tested."
            elif x1 > n1 or x2 > n2:
                # e.g. more accounts than download clicks: not a proportion, so no test applies
                r["status"] = "info"
                r["note"] = (r["note"] + " " if r["note"] else "") + \
                    "The numerator exceeds the denominator this week or in the baseline, so it was not tested as a rate."
            else:
                if n2 > 0:
                    r["baseline"] = x2 / n2
                    ref = x2 / n2
                    r["z"], r["p_value"] = two_prop_test(x1, n1, x2, n2)
                    r["ci_low"], r["ci_high"] = wilson(x1, n1)
                    rel = div(x1 / n1 - x2 / n2, x2 / n2)
                    sig = r["p_value"] < th["rate_test_alpha"]
                    big = abs(rel) >= th["wow_change_pct"] / 100
                    if sig and big:
                        r["status"] = "alert"
                        r["severity"] = "high" if r["p_value"] < th["rate_test_alpha_high"] else "medium"
                    elif big:
                        r["status"] = "watch"
                        r["note"] = (r["note"] + " " if r["note"] else "") + \
                            f"Moved {rel * 100:+.0f}% versus the 4-week rate but not statistically significant (p = {r['p_value']:.2f})."
        if r["status"] in ("alert", "watch") and ref == ref:
            sign = np.sign(cur - ref)
            r["direction"] = "neutral" if pol == 0 else ("better" if sign * pol > 0 else "worse")
            if r["status"] == "alert":
                r["status"] = {"better": "alert_good", "worse": "alert_bad"}.get(r["direction"], "alert_neutral")
        results.append(r)

    # --- Alerts. A child metric that alerts in the same direction as its parent is folded
    # into the parent's row, so one cause (e.g. a ranking jump) does not produce four alerts.
    alerts = []
    by_key = {r["key"]: r for r in results}
    folded = set()
    for parent, children in RELATED.items():
        pr = by_key.get(parent)
        if not pr or not pr["status"].startswith("alert"):
            continue
        extra = []
        for c in children:
            cr = by_key.get(c)
            if cr and cr["status"].startswith("alert") and cr["direction"] == pr["direction"]:
                folded.add(c)
                extra.append(f"{cr['label']} {fmt(cr['current'], cr['unit'])} ({fmt_change(cr['wow_pct'])})")
        if extra:
            pr["note"] = (pr["note"] + " " if pr["note"] else "") + "Same direction: " + "; ".join(extra) + "."
    for r in results:
        if r["status"].startswith("alert") and r["key"] not in folded:
            basis = (f"z = {r['z']:+.1f} vs 6-week mean {fmt(r['baseline'], r['unit'])}" if r["test"] == "volume"
                     else f"p = {r['p_value']:.4f} vs 4-week rate {fmt(r['baseline'], r['unit'])}")
            alerts.append({"type": "anomaly", "severity": r["severity"], "direction": r["direction"],
                           "metric": r["label"], "key": r["key"], "value": fmt(r["current"], r["unit"]),
                           "change": fmt_change(r["wow_pct"]), "basis": basis, "note": r["note"]})

    unchecked = []

    def guard(key, label, value, limit, worse_if, unit, basis, n=None):
        if value != value:
            return
        if launch and n is not None and n < th["min_denominator"]:
            unchecked.append(f"{label} ({n:,.0f} in the 4-week count)")
            return
        breach = value > limit if worse_if == "above" else value < limit
        if breach:
            far = value > limit * 1.5 if worse_if == "above" else value < limit / 1.5
            alerts.append({"type": "guardrail", "severity": "high" if far else "medium", "direction": "worse",
                           "metric": label, "key": key, "value": fmt(value, unit), "change": "",
                           "basis": f"limit {fmt(limit, unit)} ({worse_if} is a breach); {basis}", "note": ""})

    for p, name in PLATFORMS.items():
        if f"{p}_cpd_4w" in W:  # 4-week figure: weekly ad downloads are too few for a stable limit check
            guard(f"{p}_cpd_4w", f"{name} cost per download, 4-week", W[f"{p}_cpd_4w"].iat[t],
                  gr["max_cost_per_download_usd"], "above", "usd", "settings: max_cost_per_download_usd",
                  W[f"{p}_downloads"].iloc[max(0, t - 3):t + 1].sum())
    # In launch mode a limit is checked only once its 4-week count reaches the minimum denominator.
    guard("blended_cac_4w", "Blended CAC, 4-week", W["blended_cac_4w"].iat[t], gr["max_blended_cac_4w_usd"],
          "above", "usd", "settings: max_blended_cac_4w_usd", W["new_4w"].iat[t])
    guard("ltv_to_cac", "LTV to CAC", W["ltv_to_cac"].iat[t], gr["min_ltv_to_cac"], "below", "x",
          "settings: min_ltv_to_cac", W["new_4w"].iat[t])
    guard("refund_rate_4w_pct", "Refund rate, 4-week", W["refund_rate_4w_pct"].iat[t], gr["max_refund_rate_4w_pct"],
          "above", "pctpts", "settings: max_refund_rate_4w_pct", W["new_4w"].iat[t])
    guard("churn_monthly_pct", "Monthly churn, 4-week", W["churn_monthly_pct"].iat[t], gr["max_monthly_churn_pct"],
          "above", "pctpts", "settings: max_monthly_churn_pct", active_start.iat[t])
    if unchecked:
        dq.add("V16 launch-mode guardrails", "INFO", "all", "Not checked in launch mode until the count reaches "
               f"{th['min_denominator']}: " + "; ".join(unchecked) + ".")

    # --- Budget pacing, month to date
    month_start = week_end.replace(day=1)
    days_in_month = (month_start + pd.offsets.MonthEnd(1)).day
    elapsed = week_end.day
    pacing = []
    mtd = {}
    if ads is not None:
        for p in PLATFORMS:
            m = ads["platform"].eq(p) & ads["date"].between(month_start, week_end)
            mtd[p] = ads.loc[m, "cost_usd"].sum()
    if creators is not None:
        mtd["creators"] = creators.loc[creators["post_date"].between(month_start, week_end), "fee_usd"].sum()
    for line, budget in cfg["monthly_budgets_usd"].items():
        if line not in mtd:
            continue
        expected = budget * elapsed / days_in_month
        pace = div(mtd[line] - expected, expected)
        status = "ok"
        if abs(pace) > th["pacing_tolerance_pct"] / 100:
            status = "over" if pace > 0 else "under"
            label = {"google_ads": "Google Ads", "microsoft_ads": "Microsoft Ads", "creators": "Creators"}[line]
            alerts.append({"type": "pacing", "severity": "medium", "direction": "worse" if pace > 0 else "neutral",
                           "metric": f"{label} budget pacing", "key": f"pacing_{line}",
                           "value": f"{fmt(mtd[line], 'usd')} of {fmt(budget, 'usd')}", "change": fmt_change(pace),
                           "basis": f"expected {fmt(expected, 'usd')} after {elapsed} of {days_in_month} days "
                                    f"(tolerance {th['pacing_tolerance_pct']}%)",
                           "note": f"{fmt(budget - mtd[line], 'usd')} left for {days_in_month - elapsed} days."})
        pacing.append({"line": line, "budget": budget, "mtd": mtd[line], "expected": expected, "pace": pace,
                       "remaining": budget - mtd[line], "status": status})

    sev_rank = {"high": 0, "medium": 1, None: 2}
    dir_rank = {"worse": 0, "neutral": 1, "better": 2, None: 3}
    alerts.sort(key=lambda a: (sev_rank[a["severity"]], dir_rank[a["direction"]], a["metric"]))

    # --- Trends (OLS over the last N weeks)
    trends = []
    n = min(th["trend_weeks"], len(weeks))
    x = np.arange(n)
    for key in TREND_KEYS:
        if key not in W or n < th["min_full_weeks"]:
            continue
        y = W[key].iloc[-n:].to_numpy(dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        ss_tot = ((y - y.mean()) ** 2).sum()
        r2 = 1 - ((y - fitted) ** 2).sum() / ss_tot if ss_tot > 0 else NAN
        label, unit = next((k[1], k[2]) for k in KPIS if k[0] == key)
        trends.append({"key": key, "label": label, "unit": unit, "weeks": n, "slope_per_week": slope,
                       "pct_per_week": div(slope, y.mean()), "r2": r2})

    # --- Channel tables
    channel = []
    for p, name in PLATFORMS.items():
        if f"{p}_clicks" in W:
            channel.append({"channel": name, "spend": W[f"{p}_cost"].iat[t], "impressions": W[f"{p}_impressions"].iat[t],
                            "clicks": W[f"{p}_clicks"].iat[t], "ctr": W[f"{p}_ctr"].iat[t], "cpc": W[f"{p}_cpc"].iat[t],
                            "downloads": W[f"{p}_downloads"].iat[t], "click_to_download": W[f"{p}_click_to_download"].iat[t],
                            "cpd": W[f"{p}_cpd"].iat[t]})
    pages = []
    if sc is not None:
        wk = sc[sc["date"].between(week_start, week_end)]
        pw = sc[sc["date"].between(week_start - pd.Timedelta(days=7), week_start - pd.Timedelta(days=1))]
        # compare daily averages so a missing (lagging) day does not look like a drop
        cur_days = wk.groupby("page")["date"].nunique()
        prev_days = pw.groupby("page")["date"].nunique()
        cur_c = wk.groupby("page")["clicks"].sum()
        prev_c = pw.groupby("page")["clicks"].sum()
        imps = wk.groupby("page")["impressions"].sum()
        gpos = wk[wk["engine"] == "google"]
        gpos = (gpos["position"] * gpos["impressions"]).groupby(gpos["page"]).sum() / gpos.groupby("page")["impressions"].sum()
        for page in sorted(set(cur_c.index) | set(prev_c.index)):
            c_avg = cur_c.get(page, 0) / max(cur_days.get(page, 1), 1)
            p_avg = prev_c.get(page, 0) / max(prev_days.get(page, 1), 1)
            pages.append({"page": page, "clicks": int(cur_c.get(page, 0)), "impressions": int(imps.get(page, 0)),
                          "ctr": div(cur_c.get(page, 0), imps.get(page, 0)), "google_position": gpos.get(page, NAN),
                          "clicks_change_daily_avg": div(c_avg - p_avg, p_avg)})
        pages.sort(key=lambda r: -r["clicks"])
    creator_rows = []
    if creators is not None:
        lo_day = weeks[max(0, t - 3)]
        cp = creators[creators["post_date"].between(lo_day, week_end)]
        get4 = cf[is_get & cf["date"].between(lo_day, week_end)]
        st4 = st[st["kind"].eq("new") & st["date"].between(lo_day, week_end)]
        for name in sorted(set(cp["creator"]) | set(creators.loc[creators["slug"].isin(set(get4["source"])), "creator"])):
            slugs = set(creators.loc[creators["creator"] == name, "slug"])
            codes = {c.upper() for c in creators.loc[creators["creator"] == name, "promo_code"] if c}
            mine = cp[cp["creator"] == name]
            dls = get4.loc[get4["source"].isin(slugs), "requests"].sum()
            subs = int(st4["promo_code"].fillna("").str.upper().isin(codes).sum()) if codes else None
            fees = mine["fee_usd"].sum()
            creator_rows.append({"creator": name, "platform": ", ".join(sorted(set(creators.loc[creators['creator'] == name, 'platform']))),
                                 "posts": len(mine), "views": int(mine["views"].sum()), "fees": fees,
                                 "downloads": int(dls), "promo_subs": subs,
                                 "cost_per_download": div(fees, dls) if fees else 0.0,
                                 "cost_per_sub": div(fees, subs) if (fees and subs) else (0.0 if not fees else NAN)})
        creator_rows.sort(key=lambda r: -r["downloads"])

    last = lambda key: W[key].iat[t - 1] if t > 0 else NAN
    naive = {"installer_requests_wow": div(W["installer_requests"].iat[t] - last("installer_requests"), last("installer_requests")),
             "download_clicks_wow": div(W["dl_total"].iat[t] - last("dl_total"), last("dl_total")),
             "updater_downloads": W["updater_downloads"].iat[t], "installer_requests": W["installer_requests"].iat[t],
             "download_clicks": W["dl_total"].iat[t], "updater_method": updater_method}

    # --- Striking distance: queries ranking just off page one with many impressions, as ideas for
    # guide topics. Suggestions only; they never raise alerts.
    striking = []
    sq, sd = data["search_queries"], cfg["striking_distance"]
    if sq is not None:
        q = sq[sq["date"].between(week_end - pd.Timedelta(days=sd["days"] - 1), week_end)]
        if len(q):
            q = q.assign(wpos=q["position"] * q["impressions"])
            g = q.groupby("query").agg(impressions=("impressions", "sum"), clicks=("clicks", "sum"), wpos=("wpos", "sum"))
            g["position"] = g["wpos"] / g["impressions"].where(g["impressions"] > 0)
            top_page = q.groupby(["query", "page"])["impressions"].sum().reset_index() \
                .sort_values(["query", "impressions", "page"], ascending=[True, False, True]).drop_duplicates("query").set_index("query")["page"]
            g = g[g["position"].between(sd["min_position"], sd["max_position"]) & (g["impressions"] >= sd["min_impressions"])]
            g = g.reset_index().sort_values(["impressions", "query"], ascending=[False, True]).head(sd["top_n"])
            striking = [{"query": r.query, "impressions": int(r.impressions), "clicks": int(r.clicks), "position": r.position,
                         "top_page": top_page.get(r.query, "")} for r in g.itertuples()]

    # --- Launch mode: what happened since the data began, day by day, and by sign-up cohort.
    launch_info = None
    if launch:
        since = cf.loc[is_get, "date"].min() if is_get.any() else first_day  # the first download click
        between = lambda df, col: df[col].between(since, week_end)
        cf_sum = lambda mask: float(cf.loc[mask & between(cf, "date"), "requests"].sum())
        st_n = lambda kind: int((st["kind"].eq(kind) & between(st, "date")).sum())
        stages = [("Website page views", cf_sum(is_page)), ("Download clicks", cf_sum(is_get)),
                  ("Accounts created", float(su.loc[between(su, "date"), "accounts_created"].sum())),
                  ("New paid subscribers", float(st_n("new")))]
        funnel = []
        for i, (label, value) in enumerate(stages):
            nxt = stages[i + 1][1] if i + 1 < len(stages) else None
            lo_ci, hi_ci = wilson(nxt, value) if nxt is not None and value > 0 else (NAN, NAN)
            funnel.append({"stage": label, "count": value, "to_next": div(nxt, value) if nxt is not None else NAN,
                           "ci_low": lo_ci, "ci_high": hi_ci})
        sources = cf[is_get & between(cf, "date")].groupby("source")["requests"].sum().sort_index()
        by_day = lambda df, col, mask, val: df[mask].groupby(df.loc[mask, col])[val].sum() if df is not None else pd.Series(dtype=float)
        daily_frame = pd.DataFrame(index=pd.date_range(max(since, week_end - pd.Timedelta(days=th["launch_daily_days"] - 1)), week_end))
        for key, series in (("page_views", by_day(cf, "date", is_page, "requests")), ("downloads", by_day(cf, "date", is_get, "requests")),
                            ("accounts", by_day(su, "date", su["date"].notna(), "accounts_created")),
                            ("new_subs", st[st["kind"].eq("new")].groupby("date").size()),
                            ("cancels", st[st["kind"].eq("canceled")].groupby("date").size()),
                            ("refunds", st[st["kind"].eq("refund")].groupby("date").size()),
                            ("update_checks", by_day(cf, "date", is_update_check, "requests")),
                            ("updater_downloads", by_day(cf, "date", is_new_blockmap, "requests") if updater_method == "blockmap"
                             else (by_day(cf, "date", is_installer, "requests") - by_day(cf, "date", is_get, "requests")).clip(lower=0))):
            daily_frame[key] = series.reindex(daily_frame.index).fillna(0).astype(float)
        cohort_rows = []
        if data["cohorts"] is not None:
            for r in data["cohorts"][data["cohorts"]["cohort_week"] <= week_end].sort_values("cohort_week").itertuples():
                cohort_rows.append({"cohort_week": f"{r.cohort_week:%Y-%m-%d}", "accounts": r.accounts, "paid_7d": r.paid_7d,
                                    "paid_14d": r.paid_14d, "share_7d": div(r.paid_7d, r.accounts), "share_14d": div(r.paid_14d, r.accounts),
                                    "complete_7d": bool(r.cohort_week + pd.Timedelta(days=13) <= week_end),
                                    "complete_14d": bool(r.cohort_week + pd.Timedelta(days=20) <= week_end)})
        launch_info = {"reasons": launch_reasons, "since": f"{since:%Y-%m-%d}", "funnel": funnel,
                       "download_sources": {k: float(v) for k, v in sources.items()},
                       "daily": [{"date": f"{d:%Y-%m-%d}", **{k: float(v) for k, v in row.items()}} for d, row in daily_frame.iterrows()],
                       "cohorts": cohort_rows}

    return {"W": W, "t": t, "weeks": weeks, "week_start": week_start, "week_end": week_end, "results": results,
            "alerts": alerts, "pacing": pacing, "trends": trends, "channels": channel, "pages": pages,
            "creators": creator_rows, "inputs": inputs, "naive": naive, "notes": notes,
            "month": {"start": month_start, "elapsed": elapsed, "days": days_in_month},
            "mode": "launch" if launch else "standard", "launch": launch_info, "releases": releases,
            "release_note": release_note, "margin": margin_info, "striking": striking}


# ----------------------------------------------------------------------------- outputs
def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def write_markdown(R, cfg, dq, out_path):
    W, t = R["W"], R["t"]
    ws, we = R["week_start"], R["week_end"]
    val = lambda k: W[k].iat[t] if k in W else NAN
    L = [f"# {cfg['product']} weekly marketing report: {ws:%b %d} to {we:%b %d, %Y}", "",
         f"Built by `scripts/build_report.py` from {len(R['inputs'])} source files. Every number below comes from "
         "that script; the executive summary is the only part written by Claude.", "",
         "## Executive summary", "", "<!-- CLAUDE: replace this line with 3-5 sentences. Rules are in SKILL.md. -->", "",
         "## Alerts", ""]
    if R["alerts"]:
        L.append(md_table(["Severity", "Direction", "Metric", "Value", "Change", "Why it was flagged"],
                          [[a["severity"], a["direction"], a["metric"], a["value"], a["change"] or "",
                            a["basis"] + (f". {a['note']}" if a["note"] else "")] for a in R["alerts"]]))
    else:
        L.append("No metric crossed an alert rule this week.")
    lm = R["launch"]
    if lm:
        L += ["", "## Launch mode", "",
              f"This report is in launch mode because {', and '.join(lm['reasons'])}. Week-over-week anomaly tests are off, "
              f"and 4-week guardrails are checked only once their counts reach {cfg['thresholds']['min_denominator']}. "
              "The tables below show what happened instead.", "",
              f"### Funnel since the first download click ({pd.Timestamp(lm['since']):%b %d, %Y})", "",
              md_table(["Stage", "Count", "Conversion to next stage", "95% interval"],
                       [[f["stage"], fmt(f["count"], "count"), fmt(f["to_next"], "pct"),
                         "" if f["ci_low"] != f["ci_low"] else f"{fmt(f['ci_low'], 'pct')} to {fmt(f['ci_high'], 'pct')}"]
                        for f in lm["funnel"]]), "",
              "Download clicks by link: " + (", ".join(f"/get/{k} {fmt(v, 'count')}" for k, v in lm["download_sources"].items())
                                             or "none yet") + ".", "",
              "### Daily counts", "",
              md_table(["Date", "Page views", "Download clicks", "Accounts", "New subs", "Cancellations", "Refunds",
                        "Update checks", "Updater downloads"],
                       [[f"{pd.Timestamp(d['date']):%a %b %d}", *(fmt(d[k], "count") for k in (
                           "page_views", "downloads", "accounts", "new_subs", "cancels", "refunds", "update_checks",
                           "updater_downloads"))] for d in lm["daily"]]), "",
              "### Sign-up cohorts", ""]
        if lm["cohorts"]:
            share = lambda c, n: f"{c[f'paid_{n}d']} ({fmt(c[f'share_{n}d'], 'pct')}){'' if c[f'complete_{n}d'] else ', still open'}"
            L += [md_table(["Week starting", "Accounts", "Paid within 7 days", "Paid within 14 days"],
                           [[c["cohort_week"], c["accounts"], share(c, 7), share(c, 14)] for c in lm["cohorts"]]), "",
                  "\"Still open\" means some of that week's accounts are younger than 7 or 14 days, so the share can still rise."]
        else:
            L.append("No cohorts.csv this week (scripts/marketing/export_weekly.py writes it from Supabase).")
    L += ["", "## Funnel this week", "",
          md_table(["Stage", "This week", "Conversion to next stage", "4-week rate"],
                   [["Website page views", fmt(val("page_views"), "count"), fmt(val("visit_to_download"), "pct"),
                     fmt(next(r["baseline"] for r in R["results"] if r["key"] == "visit_to_download"), "pct")],
                    ["Download clicks", fmt(val("dl_total"), "count"), fmt(val("download_to_account"), "pct"),
                     fmt(next(r["baseline"] for r in R["results"] if r["key"] == "download_to_account"), "pct")],
                    ["Accounts created", fmt(val("accounts"), "count"), fmt(val("account_to_paid"), "pct"),
                     fmt(next(r["baseline"] for r in R["results"] if r["key"] == "account_to_paid"), "pct")],
                    ["New paid subscribers", fmt(val("new_subs"), "count"), "", ""]]), "",
          (f"Raw installer requests were {fmt(R['naive']['installer_requests'], 'count')} "
           f"({fmt_change(R['naive']['installer_requests_wow'])} week over week). "
           + (f"Installed copies started about {fmt(R['naive']['updater_downloads'], 'count')} "
              f"update download{'' if R['naive']['updater_downloads'] == 1 else 's'}, counted from requests for the newest "
              "version's blockmap file, which every update fetches and installs from the website don't. "
              if R["naive"]["updater_method"] == "blockmap" else
              f"{fmt(R['naive']['updater_downloads'], 'count')} of them did not follow a download click (installer requests "
              "minus download clicks), which is consistent with app auto-updates; this split is inferred, not directly observed. ")
           + f"New downloads (source-tagged clicks) changed {fmt_change(R['naive']['download_clicks_wow'])}."
           + (f" {R['release_note']}" if R["release_note"] else "")), ""]
    extras = [f"{label} {fmt(val(key), 'count')}" for key, label in (("email_link_opens", "\"Email me the link\" links opened"),
              ("share_link_opens", "\"Send to my PC\" links opened"), ("mac_page_views", "page views from Macs"),
              ("update_checks", "update checks by installed copies")) if key in W]
    if extras:
        L += ["Also this week: " + "; ".join(extras) + ".", ""]
    L += ["## Scorecard", ""]
    rows = []
    for r in R["results"]:
        if r["key"] == "installer_requests":
            continue
        rows.append([r["group"], r["label"], fmt(r["current"], r["unit"]), fmt(r["previous"], r["unit"]),
                     fmt_change(r["wow_pct"]), fmt(r["baseline"], r["unit"]),
                     STATUS_TEXT.get(r["status"], r["status"])])
    L.append(md_table(["Area", "Metric", "This week", "Last week", "WoW", "Baseline", "Status"], rows))
    L += ["", "Baseline = 6-week mean for counts and costs, pooled 4-week rate for percentages.", "",
          "## Channels", "", "### Search ads", ""]
    if R["channels"]:
        L.append(md_table(["Platform", "Spend", "Impr.", "Clicks", "CTR", "CPC", "Downloads", "Click to download",
                           "Cost per download"],
                          [[c["channel"], fmt(c["spend"], "usd"), fmt(c["impressions"], "count"),
                            fmt(c["clicks"], "count"), fmt(c["ctr"], "pct"), fmt(c["cpc"], "usd"),
                            fmt(c["downloads"], "count"), fmt(c["click_to_download"], "pct"), fmt(c["cpd"], "usd")]
                           for c in R["channels"]]))
    else:
        L.append("No search ads file this week.")
    L += ["", "### Organic search by page (Google + Bing)", ""]
    for note in R["notes"].values():
        L += [f"Note: {note}", ""]
    if R["pages"]:
        L.append(md_table(["Page", "Clicks", "Impressions", "CTR", "Google position", "Clicks vs last week (daily avg)"],
                          [[p["page"], fmt(p["clicks"], "count"), fmt(p["impressions"], "count"), fmt(p["ctr"], "pct"),
                            fmt(p["google_position"], "pos"), fmt_change(p["clicks_change_daily_avg"])]
                           for p in R["pages"]]))
    sd = cfg["striking_distance"]
    L += ["", "### Guide-topic suggestions (striking distance)", "",
          f"Search queries where readsharp.app averaged position {sd['min_position']} to {sd['max_position']} with at least "
          f"{sd['min_impressions']} impressions over the last {sd['days']} days. A guide written for one of these could move it "
          "onto page one. These are suggestions, never alerts.", ""]
    if R["striking"]:
        L.append(md_table(["Query", "Impressions", "Clicks", "Avg. position", "Page that ranks now"],
                          [[s["query"], fmt(s["impressions"], "count"), fmt(s["clicks"], "count"), fmt(s["position"], "pos"),
                            s["top_page"]] for s in R["striking"]]))
    else:
        L.append("No query qualifies this week" + ("." if "search_queries.csv" in R["inputs"] else
                                                   " (no search_queries.csv; export_weekly.py writes it from Search Console)."))
    L += ["", "### Creators and own posts (last 4 weeks)", ""]
    if R["creators"]:
        L.append(md_table(["Creator", "Platform", "Posts", "Views", "Fees", "Downloads", "Promo-code subs",
                           "Cost per download", "Cost per sub"],
                          [[c["creator"], c["platform"], c["posts"], fmt(c["views"], "count"), fmt(c["fees"], "usd"),
                            fmt(c["downloads"], "count"), "n/a" if c["promo_subs"] is None else c["promo_subs"],
                            fmt(c["cost_per_download"], "usd"), fmt(c["cost_per_sub"], "usd")] for c in R["creators"]]))
    else:
        L.append("No creator activity in the last 4 weeks.")
    L += ["", "## Revenue and unit economics", "",
          md_table(["Metric", "Value"],
                   [["Active subscribers", f"{fmt(val('active_subs'), 'count')} ({fmt(val('active_monthly'), 'count')} monthly, "
                                           f"{fmt(val('active_yearly'), 'count')} yearly)"],
                    ["MRR", f"{fmt(val('mrr'), 'usd')} (net new {fmt(val('net_new_mrr'), 'usd')})"],
                    ["New subscribers", f"{fmt(val('new_subs'), 'count')} ({fmt(val('yearly_share'), 'pct')} yearly)"],
                    ["Cancellations / refunds", f"{fmt(val('cancels'), 'count')} / {fmt(val('refunds_n'), 'count')}"],
                    ["Marketing spend", f"{fmt(val('spend_total'), 'usd')} (ads {fmt(val('spend_ads'), 'usd')}, "
                                        f"creators {fmt(val('creator_fees'), 'usd')})"],
                    ["Blended CAC (4-week)", fmt(val("blended_cac_4w"), "usd")],
                    ["Monthly churn (4-week)", fmt(val("churn_monthly_pct"), "pctpts")],
                    ["LTV (ARPU x margin / churn)", fmt(val("ltv_usd"), "usd")],
                    ["LTV to CAC", fmt(val("ltv_to_cac"), "x")],
                    ["CAC payback", fmt(val("payback_months"), "months")]]), "",
          f"LTV uses a gross margin of {R['margin']['value']:.0%} ({R['margin']['basis']}) and caps lifetime at 60 months.", ""]
    if R["releases"]:
        L += ["## Releases", "",
              "Cancellations and refunds in the 7 days before each release against the 7 days from its release day. "
              "The p-value is an exact test of whether both windows share one rate; small counts rarely reach significance, "
              "and a difference is an association, not proof the release caused it.", "",
              md_table(["Date", "Version", "Platforms", "Cancellations before / after", "p", "Refunds before / after", "p", "Windows"],
                       [[r["date"], r["version"], r["platforms"], f"{r['cancels_before']} / {r['cancels_after']}",
                         "n/a" if r["cancels_p"] != r["cancels_p"] else f"{r['cancels_p']:.2f}",
                         f"{r['refunds_before']} / {r['refunds_after']}",
                         "n/a" if r["refunds_p"] != r["refunds_p"] else f"{r['refunds_p']:.2f}",
                         "complete" if r["before_complete"] and r["after_complete"]
                         else "after window still open" if r["before_complete"] else "data starts inside the before window"]
                        for r in R["releases"]]), "",
              "p is shown once both 7-day windows are complete.", ""]
    L += [f"## Budget pacing, {R['month']['start']:%B} to date ({R['month']['elapsed']} of {R['month']['days']} days)", ""]
    if not R["pacing"]:
        L.append("No search ads or creator file this week, so there is no spend to pace.")
    if R["pacing"]:
        L.append(md_table(["Budget line", "Monthly budget", "Spent", "Expected by now", "Pace", "Left"],
                          [[p["line"].replace("_", " ").title(), fmt(p["budget"], "usd"), fmt(p["mtd"], "usd"),
                            fmt(p["expected"], "usd"), fmt_change(p["pace"]), fmt(p["remaining"], "usd")]
                           for p in R["pacing"]]))
    if R["trends"]:
        L += ["", f"## Trends (last {R['trends'][0]['weeks']} weeks, least-squares line)", "",
              md_table(["Metric", "Change per week", "% of average per week", "Fit (R squared)"],
                       [[tr["label"], (f"{tr['slope_per_week']:+,.1f}" if tr["unit"] == "count"
                                       else f"{'+' if tr['slope_per_week'] >= 0 else '-'}${abs(tr['slope_per_week']):,.2f}"),
                         fmt_change(tr["pct_per_week"]),
                         f"{tr['r2']:.2f}" if tr["r2"] == tr["r2"] else "n/a"] for tr in R["trends"]])]
    else:
        L += ["", "## Trends", "", f"Trends start once there are {cfg['thresholds']['min_full_weeks']} weeks of data."]
    L += ["", "## Data quality", ""]
    if dq.items:
        L.append(md_table(["Check", "Severity", "File", "Finding"],
                          [[i["check"], i["severity"], i["source"], i["message"]] for i in dq.items]))
    else:
        L.append("All checks passed.")
    L += ["", "## Method", "",
          "- Weeks run Monday to Sunday. Counts and costs are flagged when they move at least "
          f"{cfg['thresholds']['wow_change_pct']}% week over week AND sit at least {cfg['thresholds']['z_score']} standard "
          f"deviations from their {cfg['thresholds']['baseline_weeks']}-week mean.",
          "- Rates are flagged when a two-proportion z-test against the pooled 4-week rate gives p < "
          f"{cfg['thresholds']['rate_test_alpha']} AND the relative change is at least {cfg['thresholds']['wow_change_pct']}%. "
          f"Rates with fewer than {cfg['thresholds']['min_denominator']} in the denominator are not tested.",
          "- Downloads are source-tagged clicks on /get/<source> links, counted from Cloudflare's server-side logs. "
          "Update downloads by installed copies are counted separately and excluded. "
          "No cookies, pixels or in-app analytics are used.",
          "- Launch mode (fewer than 3 full weeks, or fewer than 30 page views, download clicks or accounts in the week) "
          "replaces the anomaly tests with the cumulative funnel, daily counts and sign-up cohorts.",
          "- Channel results are associations from aggregate counts, not proof that a channel caused a subscription."]
    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def write_excel(R, cfg, dq, out_path):
    from openpyxl.chart import LineChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill

    W = R["W"]
    label_of = {k[0]: k[1] for k in KPIS}
    unit_of = {k[0]: k[2] for k in KPIS}
    scorecard = pd.DataFrame([{
        "Area": r["group"], "Metric": r["label"], "This week": num(r["current"]), "Last week": num(r["previous"]),
        "WoW %": num(r["wow_pct"] * 100) if r["wow_pct"] == r["wow_pct"] else None,
        "Baseline": num(r["baseline"]), "z": num(r["z"]), "p-value": num(r["p_value"]),
        "95% CI low": num(r["ci_low"]), "95% CI high": num(r["ci_high"]),
        "Status": STATUS_TEXT.get(r["status"], r["status"]), "Note": r["note"]} for r in R["results"]])
    hist_cols = ["page_views", "dl_total", "installer_requests", "updater_downloads", "accounts", "new_subs", "active_subs", "mrr",
                 "organic_clicks", "google_ads_cost", "microsoft_ads_cost", "spend_total", "visit_to_download",
                 "download_to_account", "account_to_paid", "blended_cac_4w", "churn_monthly_pct", "ltv_to_cac"]
    hist = W[[c for c in hist_cols if c in W]].copy()
    hist.index = [f"{d:%Y-%m-%d}" for d in hist.index]
    hist.index.name = "Week starting"
    hist.columns = [label_of.get(c, c) for c in hist.columns]
    alerts = pd.DataFrame(R["alerts"] or [{"metric": "No alerts"}])
    channels = pd.DataFrame(R["channels"])
    creators = pd.DataFrame(R["creators"])
    pages = pd.DataFrame(R["pages"])
    pacing = pd.DataFrame(R["pacing"])
    quality = pd.DataFrame(dq.items or [{"check": "All checks passed"}])
    assumptions = pd.DataFrame([
        {"Setting": "Report mode", "Value": R["mode"], "Note": "; ".join(R["launch"]["reasons"]) if R["launch"] else ""},
        {"Setting": "Gross margin used", "Value": num(R["margin"]["value"]), "Note": R["margin"]["basis"]},
        {"Setting": "Gross margin (assumed)", "Value": cfg["gross_margin"], "Note": cfg.get("gross_margin_note", "")},
        {"Setting": "AI cost per million tokens (USD)", "Value": cfg["ai_cost_usd_per_million_tokens"], "Note": cfg.get("ai_cost_note", "")},
        {"Setting": "Updater downloads method", "Value": R["naive"]["updater_method"],
         "Note": "blockmap: requests for the newest version's blockmap; residual: installer requests minus download clicks"},
        {"Setting": "Prices (USD)", "Value": json.dumps(cfg["prices_usd"]), "Note": "Monthly and yearly plans"},
        {"Setting": "Starting subscribers", "Value": json.dumps(cfg["starting_active_subscribers"]), "Note": ""},
        {"Setting": "Monthly budgets (USD)", "Value": json.dumps(cfg["monthly_budgets_usd"]), "Note": ""},
        {"Setting": "Thresholds", "Value": json.dumps(cfg["thresholds"]), "Note": "Alert rules"},
        {"Setting": "Guardrails", "Value": json.dumps(cfg["guardrails"]), "Note": "Absolute limits"},
    ] + [{"Setting": f"Input {k}", "Value": v["sha256"][:16], "Note": f"{v['rows_used']} rows used"}
         for k, v in R["inputs"].items()])

    sheets = [("Scorecard", scorecard, False), ("Weekly history", hist, True), ("Alerts", alerts, False),
              ("Search ads", channels, False), ("Organic pages", pages, False), ("Creators", creators, False),
              ("Budget pacing", pacing, False), ("Data quality", quality, False), ("Assumptions", assumptions, False)]
    extra = [("Releases", pd.DataFrame(R["releases"])), ("Guide topics", pd.DataFrame(R["striking"]))]
    if R["launch"]:
        extra = [("Launch funnel", pd.DataFrame(R["launch"]["funnel"])), ("Daily", pd.DataFrame(R["launch"]["daily"])),
                 ("Cohorts", pd.DataFrame(R["launch"]["cohorts"]))] + extra
    sheets[3:3] = [(name, df, False) for name, df in extra if len(df)]
    fills = {"ALERT (worse)": "F8D7DA", "ALERT (better)": "D4EDDA", "ALERT": "FFF3CD", "Watch": "FFF3CD"}
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        for name, df, index in sheets:
            df.to_excel(xw, sheet_name=name, index=index)
            ws = xw.sheets[name]
            ws.freeze_panes = "B2" if index else "A2"
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
            if name == "Scorecard":
                status_col = list(scorecard.columns).index("Status") + 1
                for row in ws.iter_rows(min_row=2):
                    f = fills.get(row[status_col - 1].value)
                    if f:
                        for c in row:
                            c.fill = PatternFill("solid", fgColor=f)
        ws = xw.sheets["Weekly history"]
        n = len(hist) + 1
        chart = LineChart()
        chart.title = "Downloads, accounts and new subscribers by week"
        chart.y_axis.title = "Count"
        chart.height, chart.width = 8, 18
        for col_name in ("Download clicks (all sources)", "Accounts created", "New paid subscribers"):
            j = list(hist.columns).index(col_name) + 2
            chart.add_data(Reference(ws, min_col=j, min_row=1, max_row=n), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n))
        ws.add_chart(chart, f"A{n + 3}")


def write_json(R, dq, out_dir):
    W, t = R["W"], R["t"]
    clean = lambda rows: [{k: num(v) if not isinstance(v, (str, bool)) else v for k, v in row.items()} for row in rows]
    launch = R["launch"] and {**R["launch"], "funnel": clean(R["launch"]["funnel"]), "daily": clean(R["launch"]["daily"]),
                              "cohorts": clean(R["launch"]["cohorts"])}
    kpis = {
        "report_week": {"start": f"{R['week_start']:%Y-%m-%d}", "end": f"{R['week_end']:%Y-%m-%d}"},
        "mode": R["mode"],
        "launch": launch,
        "inputs": R["inputs"],
        "kpis": [{k: (num(v) if isinstance(v, (int, float, np.floating, np.integer)) else v) for k, v in r.items()}
                 for r in R["results"]],
        "funnel": {k: num(W[k].iat[t]) for k in ("page_views", "dl_total", "accounts", "new_subs", "visit_to_download",
                                                   "download_to_account", "account_to_paid")},
        "revenue": {k: num(W[k].iat[t]) for k in ("active_subs", "active_monthly", "active_yearly", "mrr", "net_new_mrr",
                                                    "cancels", "refunds_n", "spend_total", "spend_ads", "creator_fees",
                                                    "blended_cac_4w", "churn_monthly_pct", "ltv_usd", "ltv_to_cac",
                                                    "payback_months")},
        "naive_vs_clean_downloads": {k: num(v) for k, v in R["naive"].items()},
        "send_and_platform": {k: num(W[k].iat[t]) for k in ("email_link_opens", "share_link_opens", "mac_page_views",
                                                             "update_checks") if k in W},
        "gross_margin": {k: num(v) if not isinstance(v, (str, bool)) else v for k, v in R["margin"].items()},
        "releases": clean(R["releases"]),
        "striking_distance": clean(R["striking"]),
        "pacing": [{k: num(v) if not isinstance(v, str) else v for k, v in p.items()} for p in R["pacing"]],
        "trends": [{k: num(v) if not isinstance(v, str) else v for k, v in tr.items()} for tr in R["trends"]],
        "channels": [{k: num(v) if not isinstance(v, str) else v for k, v in c.items()} for c in R["channels"]],
        "creators": [{k: num(v) if not isinstance(v, str) else v for k, v in c.items()} for c in R["creators"]],
    }
    (out_dir / "kpis.json").write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    (out_dir / "alerts.json").write_text(json.dumps(R["alerts"], indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="folder with the weekly CSV exports")
    ap.add_argument("--out", required=True, help="folder for the report, workbook and JSON")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--week-end", help="Sunday that ends the report week (default: last complete week)")
    ap.add_argument("--validate-only", action="store_true")
    a = ap.parse_args()
    data_dir, out_dir = Path(a.data), Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    dq = DataQuality()
    R = build(data_dir, cfg, a.week_end, dq)
    (out_dir / "data_quality.json").write_text(json.dumps(dq.items, indent=2), encoding="utf-8")
    if R is None:
        print("BLOCKED: data problems prevent the report. See data_quality.json:")
        for i in dq.blocking():
            print(f"  [{i['check']}] {i['source']}: {i['message']}")
        sys.exit(2)
    print(f"Report week: {R['week_start']:%Y-%m-%d} to {R['week_end']:%Y-%m-%d}  ({len(R['weeks'])} weeks of history, "
          f"{R['mode']} mode)")
    print(f"Data-quality findings: {len(dq.items)}  |  Alerts: {len(R['alerts'])}")
    if a.validate_only:
        for i in dq.items:
            print(f"  [{i['severity']}] {i['check']} - {i['source']}: {i['message']}")
        return
    tag = f"{R['week_end']:%Y-%m-%d}"
    write_markdown(R, cfg, dq, out_dir / f"report_{tag}.md")
    write_excel(R, cfg, dq, out_dir / f"kpis_{tag}.xlsx")
    write_json(R, dq, out_dir)
    print(f"Wrote report_{tag}.md, kpis_{tag}.xlsx, kpis.json, alerts.json, data_quality.json to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
