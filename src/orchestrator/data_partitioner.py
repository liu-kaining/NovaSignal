"""Partition the monolithic raw_data dict into themed files + a human-readable INDEX.

The drafter agent reads INDEX.md first to navigate, then selectively `Read`s only
the data files it needs for the current chapter. This dramatically improves data
utilization vs. dumping a 500KB JSON into the context window in one shot.
"""

from __future__ import annotations

from typing import Any

# Stable ordering matters: INDEX.md rows follow this order.
_PARTITION_PLAN: list[dict[str, Any]] = [
    {
        "filename": "ipo.json",
        "title": "IPO calendar + regulatory matches",
        "chapters": "二、IPO Snapshot · 八、Synthesis",
        "source_keys": ("ipo_data", "ipo_regulatory_context", "resolved_cik"),
    },
    {
        "filename": "profile.json",
        "title": "Company profile + notes",
        "chapters": "三、Fundamentals · 五、Ownership & Management",
        "source_keys": ("company_profile", "company_notes"),
    },
    {
        "filename": "financials.json",
        "title": "Financial statements + key metrics + ratios",
        "chapters": "四、Financial Health · 八、Synthesis",
        "source_keys": ("financials", "fundamental_extras"),
    },
    {
        "filename": "ownership.json",
        "title": "Executives, float, insider trading",
        "chapters": "五、Ownership & Management",
        "source_keys": ("ownership_governance",),
    },
    {
        "filename": "sell_side.json",
        "title": "Analyst estimates, price targets, ratings",
        "chapters": "三、Fundamentals · 六、Bull Case · 八、Synthesis",
        "source_keys": ("sell_side",),
    },
    {
        "filename": "peers.json",
        "title": "Comparable companies",
        "chapters": "三、Fundamentals · 八、Synthesis (valuation)",
        "source_keys": ("comparables",),
    },
    {
        "filename": "news.json",
        "title": "Stock news + press releases",
        "chapters": "六、Bull Case · 七、Bear Case",
        "source_keys": ("news_context",),
    },
    {
        "filename": "filings.json",
        "title": "Recent SEC filings",
        "chapters": "二、IPO Snapshot · 七、Bear Case (governance)",
        "source_keys": ("sec_filings_recent",),
    },
    {
        "filename": "market.json",
        "title": "Recent EOD price action vs benchmark",
        "chapters": "八、Synthesis (first-day scenarios)",
        "source_keys": ("market_context",),
    },
    {
        "filename": "sector.json",
        "title": "Sector + industry historical performance",
        "chapters": "三、Fundamentals · 八、Synthesis",
        "source_keys": ("sector_industry_context",),
    },
    {
        "filename": "macro.json",
        "title": "Global macro + market cross-section snapshot",
        "chapters": "三、Fundamentals (industry beta) · 八、Synthesis",
        "source_keys": ("global_market_context",),
    },
    {
        "filename": "fundraising.json",
        "title": "Pre-IPO fundraising history",
        "chapters": "四、Financial Health (burn / runway)",
        "source_keys": ("fundraising",),
    },
    {
        "filename": "fetch_errors.json",
        "title": "Upstream FMP errors (which prefetch failed)",
        "chapters": "Use to know where Web research MUST fill the gap",
        "source_keys": ("fetch_errors",),
    },
]


def split_raw_data(raw_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split the canonical raw_data into themed JSON payloads.

    Returns a mapping of ``filename -> partition payload``. Always emits all
    partitions even if their source keys are empty/null, so the agent has a
    deterministic file inventory.
    """
    if not isinstance(raw_data, dict):
        raw_data = {}
    partitions: dict[str, dict[str, Any]] = {}
    meta = {
        "symbol": raw_data.get("symbol"),
        "timestamp": raw_data.get("timestamp"),
        "data_sources": raw_data.get("data_sources", []),
    }
    for spec in _PARTITION_PLAN:
        payload: dict[str, Any] = {"_meta": meta}
        for key in spec["source_keys"]:
            payload[key] = raw_data.get(key)
        partitions[spec["filename"]] = payload
    return partitions


def _availability_emoji(payload: dict[str, Any], source_keys: tuple[str, ...]) -> str:
    """Return ✅ / ⚠️ / ❌ depending on populated source-key payloads."""
    populated = 0
    total = len(source_keys)
    for key in source_keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        populated += 1
    if populated == 0:
        return "❌"
    if populated < total:
        return "⚠️"
    return "✅"


def _summarize_payload(filename: str, payload: dict[str, Any]) -> list[str]:
    """Return short human-readable bullets summarizing a partition payload."""
    lines: list[str] = []

    def add(line: str) -> None:
        lines.append(f"  - {line}")

    def is_nonempty(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, (list, dict)) and not value:
            return False
        return True

    if filename == "ipo.json":
        ipo = payload.get("ipo_data") or {}
        if isinstance(ipo, dict):
            for label, key in (
                ("date", "date"),
                ("exchange", "exchange"),
                ("priceRange", "priceRange"),
                ("shares", "shares"),
                ("marketCap", "marketCap"),
                ("company", "company"),
            ):
                val = ipo.get(key)
                if is_nonempty(val):
                    add(f"ipo_data.{label} = {val!r}")
                else:
                    add(f"ipo_data.{label} = ⚠️ missing → MUST resolve via EDGAR/HKEX")
        reg = payload.get("ipo_regulatory_context") or {}
        if isinstance(reg, dict):
            disc = reg.get("disclosure_filings_matched") or []
            pros = reg.get("prospectus_entries_matched") or []
            add(f"disclosure_filings_matched: {len(disc)} row(s)")
            add(f"prospectus_entries_matched: {len(pros)} row(s)")
        cik = payload.get("resolved_cik")
        add(f"resolved_cik = {cik!r}" if cik else "resolved_cik = null → resolve via EDGAR submissions API")

    elif filename == "profile.json":
        prof = payload.get("company_profile") or {}
        if isinstance(prof, dict) and prof:
            for k in ("companyName", "industry", "sector", "fullTimeEmployees", "ceo", "website", "description"):
                v = prof.get(k)
                if is_nonempty(v):
                    short = str(v)
                    if len(short) > 80:
                        short = short[:77] + "..."
                    add(f"company_profile.{k} = {short!r}")
                else:
                    add(f"company_profile.{k} = ⚠️ missing → WebSearch required")
        else:
            add("company_profile = ❌ empty — get EVERYTHING from EDGAR S-1 Business section")
        notes = payload.get("company_notes") or []
        add(f"company_notes: {len(notes)} entry(s)")

    elif filename == "financials.json":
        fin = payload.get("financials") or {}
        if isinstance(fin, dict):
            for tbl in (
                "income_statement_annual",
                "income_statement_quarter",
                "balance_sheet_annual",
                "cash_flow_annual",
                "key_metrics_annual",
                "ratios_annual",
                "enterprise_values_annual",
            ):
                rows = fin.get(tbl) or []
                add(
                    f"financials.{tbl}: {len(rows)} period(s)"
                    if rows
                    else f"financials.{tbl}: ❌ empty → check fetch_errors, then EDGAR S-1 Summary Financial Data"
                )
            for tbl in ("key_metrics_ttm", "ratios_ttm"):
                add(f"financials.{tbl} = " + ("populated" if is_nonempty(fin.get(tbl)) else "❌ null"))
        extras = payload.get("fundamental_extras") or {}
        if isinstance(extras, dict):
            for k in ("financial_scores", "revenue_product_segmentation", "revenue_geographic_segmentation"):
                v = extras.get(k)
                add(f"fundamental_extras.{k} = " + ("populated" if is_nonempty(v) else "❌"))

    elif filename == "ownership.json":
        own = payload.get("ownership_governance") or {}
        if isinstance(own, dict):
            execs = own.get("key_executives") or []
            add(f"key_executives: {len(execs)} entries")
            float_data = own.get("shares_float")
            add(f"shares_float = " + ("populated" if is_nonempty(float_data) else "❌ null"))
            insider_stats = own.get("insider_trading_statistics")
            add(f"insider_trading_statistics = " + ("populated" if is_nonempty(insider_stats) else "❌ null"))
            trades = own.get("insider_trades_search") or []
            add(f"insider_trades_search: {len(trades)} trades")

    elif filename == "sell_side.json":
        ss = payload.get("sell_side") or {}
        if isinstance(ss, dict):
            est = ss.get("analyst_estimates_annual") or []
            add(f"analyst_estimates_annual: {len(est)} forecast(s)")
            for k in ("price_target_summary", "price_target_consensus", "ratings_snapshot"):
                v = ss.get(k)
                add(f"{k} = " + ("populated" if is_nonempty(v) else "❌"))

    elif filename == "peers.json":
        comp = payload.get("comparables") or {}
        peers = comp.get("stock_peers") if isinstance(comp, dict) else None
        if isinstance(peers, list) and peers:
            add(f"stock_peers: {len(peers)} entries — use as starting point but validate via WebSearch")
        else:
            add("stock_peers = ❌ empty → MUST construct peer set manually via WebSearch (≥2 peers + 1 ETF)")

    elif filename == "news.json":
        nctx = payload.get("news_context") or {}
        if isinstance(nctx, dict):
            sn = nctx.get("stock_news") or []
            pr = nctx.get("press_releases") or []
            add(f"stock_news: {len(sn)} item(s)")
            add(f"press_releases: {len(pr)} item(s)")
            if not sn and not pr:
                add("Both empty → WebSearch issuer name + last 30 days")

    elif filename == "filings.json":
        fil = payload.get("sec_filings_recent") or {}
        if isinstance(fil, dict):
            rows = fil.get("filings") or []
            add(f"sec_filings_recent.filings: {len(rows)} filings")
            add(f"window: {fil.get('window_from')} → {fil.get('window_to')}")

    elif filename == "market.json":
        mkt = payload.get("market_context") or {}
        if isinstance(mkt, dict):
            ce = mkt.get("company_eod_recent") or []
            be = mkt.get("benchmark_eod_recent") or []
            quote = mkt.get("quote")
            add(f"company_eod_recent: {len(ce)} bars")
            add(f"benchmark ({mkt.get('benchmark_symbol')}) eod_recent: {len(be)} bars")
            add(f"quote = " + ("populated" if is_nonempty(quote) else "❌"))

    elif filename == "sector.json":
        sec = payload.get("sector_industry_context") or {}
        if isinstance(sec, dict):
            ident = sec.get("identifiers") or {}
            add(
                f"identifiers: sector={ident.get('sector')!r}, industry={ident.get('industry')!r}"
            )
            hs = sec.get("historical_sector_performance") or []
            hi = sec.get("historical_industry_performance") or []
            add(f"historical_sector_performance: {len(hs)} days")
            add(f"historical_industry_performance: {len(hi)} days")

    elif filename == "macro.json":
        gmc = payload.get("global_market_context") or {}
        if isinstance(gmc, dict):
            add(f"prefetch_as_of = {gmc.get('prefetch_as_of')!r}")
            for k in (
                "treasury_rates_tail",
                "economic_calendar",
                "sector_performance_snapshot",
                "industry_performance_snapshot",
                "sector_pe_snapshot",
                "industry_pe_snapshot",
                "etf_sector_weightings_spy",
                "major_index_batch_quotes",
            ):
                rows = gmc.get(k) or []
                add(f"{k}: {len(rows) if isinstance(rows, list) else 'populated'}")
            add(
                f"market_risk_premium = "
                + ("populated" if is_nonempty(gmc.get("market_risk_premium")) else "❌")
            )

    elif filename == "fundraising.json":
        fr = payload.get("fundraising") or []
        add(f"fundraising rounds: {len(fr)}")
        if not fr:
            add("Empty → search Crunchbase / press releases for prior rounds")

    elif filename == "fetch_errors.json":
        errs = payload.get("fetch_errors") or []
        add(f"fetch_errors: {len(errs)} entries")
        for err in errs[:5]:
            if isinstance(err, dict):
                add(f"  ↳ {err.get('step')}: {str(err.get('error'))[:80]}")

    return lines


def build_index_md(
    raw_data: dict[str, Any],
    partitions: dict[str, dict[str, Any]],
) -> str:
    """Build a Markdown navigation index for the sandbox data directory."""
    symbol = raw_data.get("symbol", "<unknown>")
    timestamp = raw_data.get("timestamp", "")

    lines: list[str] = []
    lines.append(f"# Data Catalog — {symbol}")
    lines.append("")
    lines.append(f"_Prefetch timestamp: {timestamp}_")
    lines.append("")
    lines.append(
        "**Read on-demand.** Open INDEX.md first, then `Read data/<filename>` only "
        "for the chapter you are currently writing. Do not Read every file upfront."
    )
    lines.append("")
    lines.append("## Quick Map — which file backs which chapter")
    lines.append("")
    lines.append("| File | Used in chapter(s) | Status |")
    lines.append("|---|---|---|")
    for spec in _PARTITION_PLAN:
        payload = partitions.get(spec["filename"], {})
        emoji = _availability_emoji(payload, spec["source_keys"])
        lines.append(
            f"| `data/{spec['filename']}` | {spec['chapters']} | {emoji} |"
        )
    lines.append("")
    lines.append(
        "Legend: ✅ populated · ⚠️ partial (some fields missing — Web research will fill) · "
        "❌ empty (Web research is mandatory for that chapter)."
    )
    lines.append("")
    lines.append("## File Contents Summary")
    lines.append("")
    for spec in _PARTITION_PLAN:
        payload = partitions.get(spec["filename"], {})
        lines.append(f"### `data/{spec['filename']}` — {spec['title']}")
        lines.append("")
        lines.extend(_summarize_payload(spec["filename"], payload))
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Research priority guidance")
    lines.append("")
    lines.append(
        "1. If `data/profile.json` shows `industry = ❌`, do NOT proceed to Chapter 三 "
        "before WebSearch + WebFetch on EDGAR S-1 to resolve sector/industry."
    )
    lines.append(
        "2. If `data/financials.json` annual statements are empty, fetch S-1 *Summary "
        "Consolidated Financial Data* and reconstruct the most recent 2 fiscal years."
    )
    lines.append(
        "3. `data/peers.json` is a starting hint only — every Bull/Bear claim about "
        "comparables MUST be backed by ≥1 cited URL in `research_notes.md`."
    )
    lines.append(
        "4. `data/fetch_errors.json` tells you EXACTLY which upstream FMP calls failed "
        "for this symbol — never claim 'data missing' on a field that was actually fetched OK."
    )
    return "\n".join(lines) + "\n"
