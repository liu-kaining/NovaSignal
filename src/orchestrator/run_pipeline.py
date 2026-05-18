"""Main pipeline orchestration entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from src.fetchers.fmp_client import FMPClient, configure_logging
from src.orchestrator.async_runner import AgentRunError
from src.orchestrator.multi_stage_runner import (
    MultiStageResult,
    StageConfig,
    execute_multi_stage,
)
from src.orchestrator.sandbox_manager import SandboxError, SandboxManager
from src.storage.r2_client import R2Client, R2StorageError, parse_report_storage_key

LOGGER = logging.getLogger(__name__)

PIPELINE_MODES = ("discovery-only", "production", "dev")


class PipelineError(RuntimeError):
    """Raised when the pipeline encounters a fatal error."""


def _fmp_prefetch_log(step: str, fn: Callable[[], Any], *, default: Any) -> Any:
    """Logged best-effort helper for **once-per-run** FMP prefetches."""
    try:
        return fn()
    except Exception as exc:
        LOGGER.warning("Global prefetch step '%s' failed: %s", step, exc)
        return default


def _prefetch_global_market_context(fmp: FMPClient) -> dict[str, Any]:
    """One call bundle per pipeline run: macro + cross-sectional sector/industry + benchmarks."""
    today = date.today()
    cal_end = today + timedelta(days=21)
    indicators: dict[str, list[Any]] = {}
    for econ_name in ("GDP", "realGDP", "inflationRate", "unemploymentRate"):
        indicators[econ_name] = _fmp_prefetch_log(
            f"economic_indicators_{econ_name}",
            lambda n=econ_name: fmp.get_economic_indicators(n),
            default=[],
        )
    return {
        "prefetch_as_of": today.isoformat(),
        "treasury_rates": _fmp_prefetch_log(
            "treasury_rates", lambda: fmp.get_treasury_rates(), default=[]
        ),
        "market_risk_premium": _fmp_prefetch_log(
            "market_risk_premium", lambda: fmp.get_market_risk_premium(), default=None
        ),
        "economic_calendar": _fmp_prefetch_log(
            "economic_calendar",
            lambda: fmp.get_economic_calendar(today, cal_end),
            default=[],
        ),
        "economic_indicators": indicators,
        "sector_performance_snapshot": _fmp_prefetch_log(
            "sector_performance_snapshot",
            lambda: fmp.get_sector_performance_snapshot(today),
            default=[],
        ),
        "industry_performance_snapshot": _fmp_prefetch_log(
            "industry_performance_snapshot",
            lambda: fmp.get_industry_performance_snapshot(today),
            default=[],
        ),
        "sector_pe_snapshot": _fmp_prefetch_log(
            "sector_pe_snapshot",
            lambda: fmp.get_sector_pe_snapshot(today),
            default=[],
        ),
        "industry_pe_snapshot": _fmp_prefetch_log(
            "industry_pe_snapshot",
            lambda: fmp.get_industry_pe_snapshot(today),
            default=[],
        ),
        "etf_sector_weightings_spy": _fmp_prefetch_log(
            "etf_sector_weightings_spy",
            lambda: fmp.get_etf_sector_weightings("SPY"),
            default=[],
        ),
        "major_index_batch_quotes": _fmp_prefetch_log(
            "batch_quote_indices",
            lambda: fmp.get_batch_quote(["^GSPC", "^VIX", "^IXIC"]),
            default=[],
        ),
    }


def run_pipeline(
    mode: str = "production",
    *,
    symbols: list[str] | None = None,
    lookback_days: int = 7,
    concurrency: int = 4,
    sandbox_timeout: float = 300,
    drafter_prompt_path: str | Path = "prompts/ipo_drafter_template.md",
    reviewer_prompt_path: str | Path = "prompts/ipo_reviewer_template.md",
    reviser_prompt_path: str | Path = "prompts/ipo_reviser_template.md",
    skip_upload: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    max_symbols: int | None = None,
    stage_config: StageConfig | None = None,
) -> dict[str, Any]:
    """Execute the NovaSignal analysis pipeline.

    Modes:
        discovery-only: Fetch IPO calendar and output discovered symbols.
        production: Full pipeline (discover → drafter → reviewer → reviser → upload).
        dev: Like production but skips R2 upload and prints results.

    The production path now runs each symbol through a 3-stage agentic flow:
        A. Drafter   — research + draft report.md + metrics.json + research_notes.md
        B. Reviewer  — independent critique → critique.md
        C. Reviser   — surgical edits per critique → final report.md / metrics.json

    Between stages, a Python-side quality gate validates outputs and can trigger
    one retry per drafting stage with the failures fed back as instructions.

    ``max_symbols`` caps the number of symbols processed in a single run (the
    PRD recommends 3–5 high-quality reports per cron, not 20 mediocre ones).

    ``skip_upload`` (production only): run agents and FMP prefetches but do not
    call R2; also skips listing R2 for symbol deduplication (for local/CI dry runs).
    """
    if mode not in PIPELINE_MODES:
        raise PipelineError(f"Invalid mode: {mode}. Must be one of {PIPELINE_MODES}")

    LOGGER.info("Starting pipeline in '%s' mode", mode)

    # Phase 1: Discovery
    fmp = _create_fmp_client()
    discovered = _discover_symbols(fmp, lookback_days, symbols)

    # When we will upload, skip symbols that already have a canonical report in R2.
    # (Skip this check if --skip-upload: dry-run without R2, or no credentials.)
    will_upload = mode == "production" and not skip_upload
    if will_upload and symbols is None:
        try:
            r2_check = _create_r2_client()
            discovered = _filter_already_processed(r2_check, discovered)
        except Exception as exc:
            LOGGER.warning("Failed to check existing reports for deduplication: %s", exc)

    if max_symbols is not None and max_symbols > 0:
        if len(discovered) > max_symbols:
            LOGGER.info(
                "Capping discovered symbols from %d to max_symbols=%d (quality > quantity)",
                len(discovered),
                max_symbols,
            )
            discovered = discovered[:max_symbols]

    symbol_names = [e["symbol"] for e in discovered]

    if mode == "discovery-only":
        LOGGER.info("Discovery-only mode: found %d symbols", len(discovered))
        _persist_discovery(discovered)
        return {"mode": mode, "symbols": symbol_names, "results": []}

    # Phase 2: Load prompt templates (drafter / reviewer / reviser)
    drafter_prompt = _load_prompt(drafter_prompt_path)
    reviewer_prompt = _load_prompt(reviewer_prompt_path)
    reviser_prompt = _load_prompt(reviser_prompt_path)

    prefetch_errs: list[dict[str, str]] = []
    ipo_regulatory_lists: dict[str, list[dict[str, Any]]] = {
        "disclosures": _fmp_safe_call(
            "ipos_disclosure_prefetch",
            prefetch_errs,
            lambda: fmp.get_ipos_disclosure(),
            default=[],
        ),
        "prospectuses": _fmp_safe_call(
            "ipos_prospectus_prefetch",
            prefetch_errs,
            lambda: fmp.get_ipos_prospectus(),
            default=[],
        ),
    }
    for err in prefetch_errs:
        LOGGER.warning(
            "IPO regulatory prefetch step '%s' failed: %s",
            err.get("step"),
            err.get("error"),
        )

    global_market_context = _prefetch_global_market_context(fmp)

    # Phase 3: Execute agent for each symbol
    r2 = _create_r2_client() if will_upload else None
    sandbox_mgr = SandboxManager()

    if will_upload and r2:
        try:
            r2.upload_fmp_prefetch_bundle(
                {
                    "ipo_regulatory_lists": ipo_regulatory_lists,
                    "global_market_context": global_market_context,
                }
            )
        except R2StorageError as exc:
            LOGGER.warning("Could not upload FMP prefetch bundle to R2: %s", exc)

    results = asyncio.run(
        _run_all_agents(
            discovery_entries=discovered,
            fmp=fmp,
            drafter_prompt=drafter_prompt,
            reviewer_prompt=reviewer_prompt,
            reviser_prompt=reviser_prompt,
            sandbox_mgr=sandbox_mgr,
            r2=r2,
            concurrency=concurrency,
            timeout=sandbox_timeout,
            model=model,
            base_url=base_url,
            ipo_regulatory_lists=ipo_regulatory_lists,
            global_market_context=global_market_context,
            stage_config=stage_config,
        )
    )

    summary = {
        "mode": mode,
        "symbols": symbol_names,
        "results": results,
        "success_count": sum(1 for r in results if r.get("success")),
        "failure_count": sum(1 for r in results if not r.get("success")),
    }

    if mode == "dev":
        _print_dev_results(results)

    LOGGER.info(
        "Pipeline complete: %d/%d succeeded",
        summary["success_count"],
        len(results),
    )
    return summary


async def _run_all_agents(
    *,
    discovery_entries: list[dict[str, Any]],
    fmp: FMPClient,
    drafter_prompt: str,
    reviewer_prompt: str,
    reviser_prompt: str,
    sandbox_mgr: SandboxManager,
    r2: R2Client | None,
    concurrency: int,
    timeout: float,
    model: str | None = None,
    base_url: str | None = None,
    ipo_regulatory_lists: dict[str, list[dict[str, Any]]] | None = None,
    global_market_context: dict[str, Any] | None = None,
    stage_config: StageConfig | None = None,
) -> list[dict[str, Any]]:
    """Run multi-stage pipelines with bounded concurrency.

    With three Claude invocations per symbol, the effective per-symbol wall time
    is ~6–10 min. Concurrency therefore should usually be modest (1–3).
    """
    semaphore = asyncio.Semaphore(concurrency)
    ipo_lists = ipo_regulatory_lists or {"disclosures": [], "prospectuses": []}
    gmc = global_market_context
    tasks = [
        _run_single_pipeline(
            entry=entry,
            fmp=fmp,
            drafter_prompt=drafter_prompt,
            reviewer_prompt=reviewer_prompt,
            reviser_prompt=reviser_prompt,
            sandbox_mgr=sandbox_mgr,
            r2=r2,
            timeout=timeout,
            semaphore=semaphore,
            model=model,
            base_url=base_url,
            ipo_regulatory_lists=ipo_lists,
            global_market_context=gmc,
            stage_config=stage_config,
        )
        for entry in discovery_entries
    ]
    return await asyncio.gather(*tasks)


async def _run_single_pipeline(
    *,
    entry: dict[str, Any],
    fmp: FMPClient,
    drafter_prompt: str,
    reviewer_prompt: str,
    reviser_prompt: str,
    sandbox_mgr: SandboxManager,
    r2: R2Client | None,
    timeout: float,
    semaphore: asyncio.Semaphore,
    model: str | None = None,
    base_url: str | None = None,
    ipo_regulatory_lists: dict[str, list[dict[str, Any]]] | None = None,
    global_market_context: dict[str, Any] | None = None,
    stage_config: StageConfig | None = None,
) -> dict[str, Any]:
    """Execute the 3-stage Drafter → Reviewer → Reviser pipeline for one symbol.

    Uploads all artifacts (report, metrics, research_notes, critique,
    quality_gate trace) to R2 when ``r2`` is provided.
    """
    symbol = entry["symbol"]
    async with semaphore:
        try:
            raw_data = _build_raw_data(
                entry,
                fmp,
                ipo_regulatory_lists=ipo_regulatory_lists,
                global_market_context=global_market_context,
            )
            if r2:
                _safe_upload(r2.upload_raw_data, symbol, raw_data, label="raw_data")

            env_vars: dict[str, str] = {
                "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
            }
            effective_base_url = base_url or os.getenv("ANTHROPIC_BASE_URL")
            if effective_base_url:
                env_vars["ANTHROPIC_BASE_URL"] = effective_base_url

            cfg = stage_config or _stage_config_from_settings(timeout)

            stage_result: MultiStageResult = await execute_multi_stage(
                symbol=symbol,
                raw_data=raw_data,
                drafter_prompt=drafter_prompt,
                reviewer_prompt=reviewer_prompt,
                reviser_prompt=reviser_prompt,
                sandbox_mgr=sandbox_mgr,
                env_vars=env_vars,
                model=model,
                stage_config=cfg,
            )

            if r2:
                if stage_result.report:
                    _safe_upload(r2.upload_report, symbol, stage_result.report, label="report")
                if stage_result.metrics:
                    _safe_upload(r2.upload_metrics, symbol, stage_result.metrics, label="metrics")
                if stage_result.research_notes:
                    _safe_upload(
                        r2.upload_research_notes,
                        symbol,
                        stage_result.research_notes,
                        label="research_notes",
                    )
                if stage_result.critique:
                    _safe_upload(
                        r2.upload_critique,
                        symbol,
                        stage_result.critique,
                        label="critique",
                    )
                _safe_upload(
                    r2.upload_quality_gate,
                    symbol,
                    stage_result.as_summary(),
                    label="quality_gate",
                )
                # On failure (incl. timeouts), upload whatever partial state
                # the agent managed to write so we can post-mortem.
                if stage_result.partial_artifacts:
                    _upload_debug_artifacts(
                        r2, symbol, stage_result.partial_artifacts
                    )

            LOGGER.info(
                "[%s] multi-stage complete: success=%s final_gate_passed=%s",
                symbol,
                stage_result.success,
                stage_result.final_gate_passed,
            )
            return {
                "symbol": symbol,
                "success": stage_result.success,
                "final_gate_passed": stage_result.final_gate_passed,
                "report": stage_result.report,
                "metrics": stage_result.metrics,
                "research_notes_chars": len(stage_result.research_notes or ""),
                "critique_chars": len(stage_result.critique or ""),
                "stage_outcomes": [
                    {
                        "stage": o.stage,
                        "attempt": o.attempt,
                        "success": o.success,
                        "duration_seconds": o.duration_seconds,
                        "error": o.error,
                    }
                    for o in stage_result.outcomes
                ],
                "error": stage_result.error,
            }

        except (AgentRunError, SandboxError) as exc:
            LOGGER.error("Agent run failed for %s: %s", symbol, exc)
            return {"symbol": symbol, "success": False, "error": str(exc)}
        except R2StorageError as exc:
            LOGGER.error("Upload failed for %s: %s", symbol, exc)
            return {"symbol": symbol, "success": False, "error": f"Upload: {exc}"}


def _safe_upload(
    fn: Callable[..., str], symbol: str, payload: Any, *, label: str
) -> str | None:
    """Best-effort R2 upload — never aborts pipeline."""
    try:
        return fn(symbol, payload)
    except R2StorageError as exc:
        LOGGER.warning("R2 upload (%s) failed for %s: %s", label, symbol, exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Unexpected upload error (%s) for %s: %s", label, symbol, exc)
    return None


def _upload_debug_artifacts(
    r2: R2Client, symbol: str, partials: dict[str, str]
) -> None:
    """Upload partial sandbox state captured on stage failure for post-mortem.

    Keys are written under ``debug/{date}/{SYMBOL}/{drafter|reviewer}/{filename}``.
    """
    LOGGER.info(
        "[%s] uploading %d debug artifact(s) to R2 debug/ path",
        symbol,
        len(partials),
    )
    for rel_path, content in partials.items():
        ct = "text/markdown"
        if rel_path.endswith(".json"):
            ct = "application/json"
        elif rel_path.endswith(".log"):
            ct = "text/plain"
        try:
            r2.upload_debug_artifact(symbol, rel_path, content, content_type=ct)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Failed to upload debug artifact %s for %s: %s",
                rel_path,
                symbol,
                exc,
            )


def _stage_config_from_settings(default_per_stage_timeout: float) -> StageConfig:
    """Derive StageConfig from config/settings.yaml (with safe fallbacks)."""
    try:
        from config.loader import get_pipeline_settings

        pipe = get_pipeline_settings()
    except Exception:
        pipe = {}

    stages = pipe.get("stages") or {}
    return StageConfig(
        drafter_timeout=float(stages.get("drafter_timeout_seconds", default_per_stage_timeout)),
        reviewer_timeout=float(stages.get("reviewer_timeout_seconds", default_per_stage_timeout * 0.7)),
        reviser_timeout=float(stages.get("reviser_timeout_seconds", default_per_stage_timeout * 0.85)),
        drafter_max_attempts=int(stages.get("drafter_max_attempts", 2)),
        reviser_max_attempts=int(stages.get("reviser_max_attempts", 2)),
        url_verify_sample=int(stages.get("url_verify_sample", 5)),
        url_verify_timeout=float(stages.get("url_verify_timeout", 5)),
        skip_url_verification=bool(stages.get("skip_url_verification", False)),
    )


def _discover_symbols(
    fmp: FMPClient, lookback_days: int, override: list[str] | None
) -> list[dict[str, Any]]:
    """Discover symbols with IPO data from FMP calendar or use override list.

    Returns a list of dicts with at minimum a 'symbol' key plus any available
    IPO calendar metadata.
    """
    if override:
        return [{"symbol": s.upper()} for s in override]

    today = date.today()
    from_date = today - timedelta(days=lookback_days)
    entries = fmp.get_ipo_calendar(from_date, today)
    discovered = [e for e in entries if e.get("symbol")]
    LOGGER.info("Discovered %d symbols from IPO calendar", len(discovered))
    return discovered


def _tail_list(rows: Any, max_items: int) -> list[Any]:
    if not isinstance(rows, list) or max_items <= 0:
        return []
    if len(rows) <= max_items:
        return rows
    return rows[-max_items:]


def _sector_industry_from_profile(
    profile: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not isinstance(profile, dict):
        return None, None

    def _clean(val: Any) -> str | None:
        if val is None:
            return None
        t = str(val).strip()
        return t if t else None

    sec = _clean(profile.get("sector") or profile.get("sectorFormatted"))
    ind = _clean(profile.get("industry") or profile.get("industryFormatted"))
    return sec, ind


def _compact_global_market_for_raw(cache: dict[str, Any]) -> dict[str, Any]:
    """Shrink prefetch payload for sandbox JSON; keep cross-section + macro tails."""
    tr = cache.get("treasury_rates") or []
    if not isinstance(tr, list):
        tr = []
    cal = cache.get("economic_calendar") or []
    if not isinstance(cal, list):
        cal = []
    out_ind: dict[str, Any] = {}
    raw_ind = cache.get("economic_indicators") or {}
    if isinstance(raw_ind, dict):
        for k, series in raw_ind.items():
            if isinstance(series, list):
                out_ind[str(k)] = _tail_list(series, 14)
            else:
                out_ind[str(k)] = series
    ssec = cache.get("sector_performance_snapshot") or []
    if not isinstance(ssec, list):
        ssec = []
    inds = cache.get("industry_performance_snapshot") or []
    if not isinstance(inds, list):
        inds = []
    spe = cache.get("sector_pe_snapshot") or []
    if not isinstance(spe, list):
        spe = []
    ipe = cache.get("industry_pe_snapshot") or []
    if not isinstance(ipe, list):
        ipe = []
    return {
        "prefetch_as_of": cache.get("prefetch_as_of"),
        "notes": (
            "Pipeline-wide snapshot for the run: Treasury curve tail, risk premium, "
            "macro release calendar window, indicator series (trimmed), market-wide "
            "sector/industry performance & P/E snapshots, SPY sector weights, "
            "and major index batch quotes."
        ),
        "treasury_rates_tail": _tail_list(tr, 30),
        "market_risk_premium": cache.get("market_risk_premium"),
        "economic_calendar": cal[:50] if len(cal) > 50 else cal,
        "economic_indicators_trimmed": out_ind,
        "sector_performance_snapshot": ssec[:60] if len(ssec) > 60 else ssec,
        "industry_performance_snapshot": inds[:80] if len(inds) > 80 else inds,
        "sector_pe_snapshot": spe[:40] if len(spe) > 40 else spe,
        "industry_pe_snapshot": ipe[:80] if len(ipe) > 80 else ipe,
        "etf_sector_weightings_spy": cache.get("etf_sector_weightings_spy") or [],
        "major_index_batch_quotes": cache.get("major_index_batch_quotes") or [],
    }


def _build_raw_data(
    entry: dict[str, Any],
    fmp: FMPClient,
    *,
    ipo_regulatory_lists: dict[str, list[dict[str, Any]]] | None = None,
    global_market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ``raw_data.json`` for the agent: IPO row + rich FMP enrichment.

    Targets **FMP Premium-class** datasets (full fundamentals, calendars, SEC
    search, analyst modules, IPO disclosure listings—subject to account limits).

    Best-effort: individual FMP failures are logged, recorded under ``fetch_errors``,
    and do not fail the pipeline.
    """
    symbol = entry["symbol"]
    errors: list[dict[str, str]] = []
    sources: list[str] = ["fmp_ipo_calendar_row"]
    ipo_lists = ipo_regulatory_lists or {"disclosures": [], "prospectuses": []}

    raw_data: dict[str, Any] = {
        "symbol": symbol,
        "timestamp": date.today().isoformat(),
        "data_sources": sources,
        "ipo_data": {k: v for k, v in entry.items() if k != "symbol"},
        "fetch_errors": [],
    }

    profile = _fmp_safe_call(
        "company_profile",
        errors,
        lambda: fmp.get_company_profile(symbol),
        default=None,
    )
    if profile:
        raw_data["company_profile"] = profile
        sources.append("fmp_profile")
    else:
        raw_data["company_profile"] = None

    company_notes = _fmp_safe_call(
        "company_notes",
        errors,
        lambda: fmp.get_company_notes(symbol),
        default=[],
    )
    raw_data["company_notes"] = company_notes
    if company_notes:
        sources.append("fmp_company_notes")

    cik = _normalize_cik(entry.get("cik"))
    if not cik and isinstance(profile, dict):
        cik = _normalize_cik(profile.get("cik"))
    raw_data["resolved_cik"] = cik

    fundraising: list[Any] = []
    if cik:
        fundraising = _fmp_safe_call(
            "fundraising",
            errors,
            lambda: _normalize_fundraising(fmp.get_fundraising(cik)),
            default=[],
        )
        if fundraising:
            sources.append("fmp_fundraising")
    raw_data["fundraising"] = fundraising

    sector, industry = _sector_industry_from_profile(profile)
    hist_sec = _fmp_safe_call(
        "historical_sector_performance",
        errors,
        lambda: _tail_list(fmp.get_historical_sector_performance(sector), 40)
        if sector
        else [],
        default=[],
    )
    hist_ind = _fmp_safe_call(
        "historical_industry_performance",
        errors,
        lambda: _tail_list(fmp.get_historical_industry_performance(industry), 40)
        if industry
        else [],
        default=[],
    )
    raw_data["sector_industry_context"] = {
        "identifiers": {"sector": sector, "industry": industry},
        "historical_sector_performance": hist_sec,
        "historical_industry_performance": hist_ind,
        "notes": (
            "Names from FMP company profile; use for tape / sector rotation context "
            "and future earnings-analysis templates."
        ),
    }
    if hist_sec or hist_ind:
        sources.append("fmp_sector_industry_history")

    financials = {
        "income_statement_annual": _fmp_safe_call(
            "income_statement_annual",
            errors,
            lambda: fmp.get_income_statement(symbol, period="annual", limit=8),
            default=[],
        ),
        "income_statement_quarter": _fmp_safe_call(
            "income_statement_quarter",
            errors,
            lambda: fmp.get_income_statement(symbol, period="quarter", limit=8),
            default=[],
        ),
        "balance_sheet_annual": _fmp_safe_call(
            "balance_sheet_annual",
            errors,
            lambda: fmp.get_balance_sheet_statement(symbol, period="annual", limit=6),
            default=[],
        ),
        "cash_flow_annual": _fmp_safe_call(
            "cash_flow_annual",
            errors,
            lambda: fmp.get_cash_flow_statement(symbol, period="annual", limit=6),
            default=[],
        ),
        "key_metrics_annual": _fmp_safe_call(
            "key_metrics_annual",
            errors,
            lambda: fmp.get_key_metrics(symbol, period="annual", limit=6),
            default=[],
        ),
        "ratios_annual": _fmp_safe_call(
            "ratios_annual",
            errors,
            lambda: fmp.get_ratios(symbol, period="annual", limit=6),
            default=[],
        ),
        "enterprise_values_annual": _fmp_safe_call(
            "enterprise_values_annual",
            errors,
            lambda: fmp.get_enterprise_values(symbol, period="annual", limit=6),
            default=[],
        ),
        "key_metrics_ttm": _fmp_safe_call(
            "key_metrics_ttm",
            errors,
            lambda: fmp.get_key_metrics_ttm(symbol),
            default=None,
        ),
        "ratios_ttm": _fmp_safe_call(
            "ratios_ttm", errors, lambda: fmp.get_ratios_ttm(symbol), default=None
        ),
    }
    raw_data["financials"] = financials
    if _financials_non_empty(financials):
        sources.append("fmp_financial_statements")

    hist_from, hist_to = _historical_window(entry)
    company_eod = _fmp_safe_call(
        "historical_prices_company",
        errors,
        lambda: fmp.get_stock_price_historical(symbol, hist_from, hist_to),
        default=[],
    )
    benchmark_eod = _fmp_safe_call(
        "historical_prices_spy",
        errors,
        lambda: fmp.get_stock_price_historical("SPY", hist_from, hist_to),
        default=[],
    )
    if company_eod:
        sources.append("fmp_historical_eod")
    quote = _fmp_safe_call("quote", errors, lambda: fmp.get_quote(symbol), default=None)
    if quote:
        sources.append("fmp_quote")
    raw_data["market_context"] = {
        "benchmark_symbol": "SPY",
        "window_from": hist_from.isoformat(),
        "window_to": hist_to.isoformat(),
        "notes": (
            "Recent trading days only (trimmed) for JSON size; use full FMP series "
            "in upstream if needed."
        ),
        "company_eod_recent": _trim_eod_rows(company_eod, max_rows=72),
        "benchmark_eod_recent": _trim_eod_rows(benchmark_eod, max_rows=72),
        "quote": quote,
    }

    news_context = {
        "stock_news": _fmp_safe_call(
            "stock_news",
            errors,
            lambda: fmp.get_stock_news(symbol, limit=30),
            default=[],
        ),
        "press_releases": _fmp_safe_call(
            "press_releases",
            errors,
            lambda: fmp.get_press_releases(symbol, limit=25),
            default=[],
        ),
    }
    raw_data["news_context"] = news_context
    if news_context["stock_news"] or news_context["press_releases"]:
        sources.append("fmp_news")

    raw_data["comparables"] = {
        "stock_peers": _fmp_safe_call(
            "stock_peers", errors, lambda: fmp.get_stock_peers(symbol), default=[]
        ),
    }
    if raw_data["comparables"]["stock_peers"]:
        sources.append("fmp_stock_peers")

    ownership_governance = {
        "key_executives": _fmp_safe_call(
            "key_executives",
            errors,
            lambda: fmp.get_key_executives(symbol),
            default=[],
        ),
        "shares_float": _fmp_safe_call(
            "shares_float", errors, lambda: fmp.get_shares_float(symbol), default=None
        ),
        "insider_trading_statistics": _fmp_safe_call(
            "insider_trading_statistics",
            errors,
            lambda: fmp.get_insider_trading_statistics(symbol),
            default=None,
        ),
        "insider_trades_search": _fmp_safe_call(
            "insider_trading_search",
            errors,
            lambda: fmp.get_insider_trading_search(symbol, limit=35),
            default=[],
        ),
    }
    raw_data["ownership_governance"] = ownership_governance
    if any(
        ownership_governance.get(k)
        for k in (
            "key_executives",
            "shares_float",
            "insider_trading_statistics",
            "insider_trades_search",
        )
    ):
        sources.append("fmp_ownership_governance")

    sell_side = {
        "analyst_estimates_annual": _fmp_safe_call(
            "analyst_estimates_annual",
            errors,
            lambda: fmp.get_analyst_estimates(symbol, period="annual", limit=12),
            default=[],
        ),
        "price_target_summary": _fmp_safe_call(
            "price_target_summary",
            errors,
            lambda: fmp.get_price_target_summary(symbol),
            default=None,
        ),
        "price_target_consensus": _fmp_safe_call(
            "price_target_consensus",
            errors,
            lambda: fmp.get_price_target_consensus(symbol),
            default=None,
        ),
        "ratings_snapshot": _fmp_safe_call(
            "ratings_snapshot",
            errors,
            lambda: fmp.get_ratings_snapshot(symbol),
            default=None,
        ),
    }
    raw_data["sell_side"] = sell_side
    if any(
        sell_side.get(k)
        for k in (
            "analyst_estimates_annual",
            "price_target_summary",
            "price_target_consensus",
            "ratings_snapshot",
        )
    ):
        sources.append("fmp_sell_side")

    fundamental_extras = {
        "financial_scores": _fmp_safe_call(
            "financial_scores",
            errors,
            lambda: fmp.get_financial_scores(symbol),
            default=None,
        ),
        "revenue_product_segmentation": _fmp_safe_call(
            "revenue_product_segmentation",
            errors,
            lambda: fmp.get_revenue_product_segmentation(symbol),
            default=[],
        ),
        "revenue_geographic_segmentation": _fmp_safe_call(
            "revenue_geographic_segmentation",
            errors,
            lambda: fmp.get_revenue_geographic_segmentation(symbol),
            default=[],
        ),
    }
    raw_data["fundamental_extras"] = fundamental_extras
    if any(
        fundamental_extras.get(k)
        for k in (
            "financial_scores",
            "revenue_product_segmentation",
            "revenue_geographic_segmentation",
        )
    ):
        sources.append("fmp_fundamental_extras")

    sec_window_end = date.today()
    sec_window_start = sec_window_end - timedelta(days=730)
    raw_data["sec_filings_recent"] = {
        "window_from": sec_window_start.isoformat(),
        "window_to": sec_window_end.isoformat(),
        "filings": _fmp_safe_call(
            "sec_filings_symbol",
            errors,
            lambda: fmp.get_sec_filings_symbol(
                symbol, sec_window_start, sec_window_end, limit=40
            ),
            default=[],
        ),
    }
    if raw_data["sec_filings_recent"]["filings"]:
        sources.append("fmp_sec_filings")

    disc_rows = _match_ipo_regulatory_rows(
        ipo_lists.get("disclosures") or [], symbol, cik, max_rows=60
    )
    pros_rows = _match_ipo_regulatory_rows(
        ipo_lists.get("prospectuses") or [], symbol, cik, max_rows=60
    )
    raw_data["ipo_regulatory_context"] = {
        "notes": (
            "Rows matched from pipeline-level FMP ipos-disclosure / ipos-prospectus "
            "snapshots (same-day); field names vary—prefer links to SEC."
        ),
        "disclosure_filings_matched": disc_rows,
        "prospectus_entries_matched": pros_rows,
    }
    if disc_rows or pros_rows:
        sources.append("fmp_ipo_regulatory_lists")

    if global_market_context:
        raw_data["global_market_context"] = _compact_global_market_for_raw(
            global_market_context
        )
        sources.append("fmp_global_market_prefetch")
    else:
        raw_data["global_market_context"] = None

    raw_data["fetch_errors"] = errors
    return raw_data


def _match_ipo_regulatory_rows(
    rows: list[dict[str, Any]],
    symbol: str,
    cik: str | None,
    *,
    max_rows: int,
) -> list[dict[str, Any]]:
    """Select disclosure/prospectus rows that likely belong to ``symbol`` / ``cik``."""
    if not rows or max_rows <= 0:
        return []
    sym_u = symbol.strip().upper()
    cik_n = _normalize_cik(cik) if cik else None
    matched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r_sym = str(
            row.get("symbol")
            or row.get("ticker")
            or row.get("companySymbol")
            or row.get("stock")
            or ""
        ).strip().upper()
        r_cik = _normalize_cik(row.get("cik"))
        if r_sym == sym_u or (cik_n and r_cik == cik_n):
            matched.append(row)
        if len(matched) >= max_rows:
            break
    return matched


def _fmp_safe_call(
    step: str,
    errors: list[dict[str, str]],
    fn: Callable[[], Any],
    *,
    default: Any,
) -> Any:
    try:
        return fn()
    except Exception as exc:
        LOGGER.warning("FMP step '%s' failed: %s", step, exc)
        errors.append({"step": step, "error": str(exc)})
        return default


def _normalize_cik(value: Any) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    if s.isdigit():
        return s.zfill(10)
    return s


def _normalize_fundraising(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


def _financials_non_empty(financials: dict[str, Any]) -> bool:
    list_keys = (
        "income_statement_annual",
        "income_statement_quarter",
        "balance_sheet_annual",
        "cash_flow_annual",
        "key_metrics_annual",
        "ratios_annual",
        "enterprise_values_annual",
    )
    for k in list_keys:
        block = financials.get(k)
        if isinstance(block, list) and len(block) > 0:
            return True
    ttm_km = financials.get("key_metrics_ttm")
    ttm_r = financials.get("ratios_ttm")
    if isinstance(ttm_km, list):
        if len(ttm_km) > 0:
            return True
    elif ttm_km:
        return True
    if isinstance(ttm_r, list):
        if len(ttm_r) > 0:
            return True
    elif ttm_r:
        return True
    return False


def _parse_iso_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _historical_window(entry: dict[str, Any]) -> tuple[date, date]:
    today = date.today()
    ipo_hint = _parse_iso_date(entry.get("date")) or _parse_iso_date(
        entry.get("expectedDate")
    ) or _parse_iso_date(entry.get("expected_date"))
    if ipo_hint:
        start = ipo_hint - timedelta(days=800)
    else:
        start = today - timedelta(days=420)
    if start >= today:
        start = today - timedelta(days=120)
    return start, today


def _trim_eod_rows(rows: list[dict[str, Any]], *, max_rows: int) -> list[dict[str, Any]]:
    if not rows or max_rows <= 0:
        return []
    sorted_rows = sorted(rows, key=lambda r: str(r.get("date") or ""), reverse=True)
    return sorted_rows[:max_rows]


def _persist_discovery(entries: list[dict[str, Any]]) -> None:
    """Write discovery results to a local JSON file for downstream consumption."""
    output_path = Path("discovery_output.json")
    payload = {
        "discovery_date": date.today().isoformat(),
        "count": len(entries),
        "symbols": [e["symbol"] for e in entries],
        "entries": entries,
    }
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    LOGGER.info("Persisted discovery results to %s (%d entries)", output_path, len(entries))


def _load_prompt(path: str | Path) -> str:
    """Load prompt template from file."""
    prompt_file = Path(path)
    if not prompt_file.exists():
        raise PipelineError(f"Prompt template not found: {path}")
    return prompt_file.read_text(encoding="utf-8")


def _create_fmp_client() -> FMPClient:
    """Create FMP client from config/settings.yaml (API key still from env)."""
    try:
        return FMPClient.from_config()
    except Exception as exc:
        LOGGER.warning("FMP from_config failed (%s); using constructor defaults", exc)
        return FMPClient()


def _create_r2_client() -> R2Client:
    """Create R2 client from environment."""
    return R2Client()


def _filter_already_processed(
    r2: R2Client, discovered: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Filter out symbols that already have **any** report object in R2.

    Lists ``reports/`` and treats a ticker as done if **any** dated key exists
    (e.g. ``reports/2026-01-01/AAPL_report.md``), not only "today". That
    stops auto-discovery from re-running the same symbol forever after the first
    successful report; use explicit ``--symbols`` for deliberate re-runs.

    Per-object uploads still use **overwrite** semantics for a fixed key (same
    date + symbol → one ``put_object``, no duplicate keys on retry).
    """
    LOGGER.info("Checking R2 for already processed symbols...")
    existing_keys = r2.list_objects("reports/")
    processed_symbols: set[str] = set()
    for k in existing_keys:
        parsed = parse_report_storage_key(k)
        if parsed:
            _date_str, sym = parsed
            processed_symbols.add(sym.upper())
    LOGGER.debug("Found %d existing reports in R2", len(processed_symbols))

    filtered = [e for e in discovered if e["symbol"] not in processed_symbols]
    LOGGER.info("Deduplication: %d new symbols to process (filtered %d already done)",
                len(filtered), len(discovered) - len(filtered))
    return filtered


def _print_dev_results(results: list[dict[str, Any]]) -> None:
    """Print results to stdout in dev mode."""
    for r in results:
        print(f"\n{'='*60}")
        print(f"Symbol: {r['symbol']} | Success: {r.get('success')}")
        if r.get("error"):
            print(f"Error: {r['error']}")
        if r.get("report"):
            print(f"Report preview: {r['report'][:200]}...")
        print(f"{'='*60}")


def _first_nonempty(*values: object) -> str | None:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def main() -> None:
    """CLI entry point for run_pipeline."""
    try:
        from config.loader import get_pipeline_settings

        pipe = get_pipeline_settings()
    except Exception:
        pipe = {}

    parser = argparse.ArgumentParser(description="NovaSignal Analysis Pipeline")
    parser.add_argument(
        "--mode",
        choices=PIPELINE_MODES,
        default="production",
        help="Pipeline execution mode",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Override symbols to analyze (space-separated)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Days to look back for IPO discovery",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(pipe.get("concurrency", 4)),
        help="Max concurrent agent invocations",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(pipe.get("sandbox_timeout_seconds", 300)),
        help="Sandbox timeout in seconds",
    )
    parser.add_argument(
        "--drafter-prompt",
        default="prompts/ipo_drafter_template.md",
        help="Path to drafter (Stage A) prompt template",
    )
    parser.add_argument(
        "--reviewer-prompt",
        default="prompts/ipo_reviewer_template.md",
        help="Path to reviewer (Stage B) prompt template",
    )
    parser.add_argument(
        "--reviser-prompt",
        default="prompts/ipo_reviser_template.md",
        help="Path to reviser (Stage C) prompt template",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=int(pipe.get("max_symbols_per_run", 5)),
        help="Cap on symbols processed per run (quality > quantity; default 5)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Claude model to use (default: CLI default; opus-4-x recommended for production)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Anthropic API base URL (default: official API)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Production: run pipeline but skip R2 uploads and skip R2-based symbol deduplication",
    )

    args = parser.parse_args()
    configure_logging()

    model = _first_nonempty(args.model, os.getenv("ANTHROPIC_MODEL"), pipe.get("model"))
    base_url = _first_nonempty(
        args.base_url, os.getenv("ANTHROPIC_BASE_URL"), pipe.get("base_url")
    )

    try:
        summary = run_pipeline(
            mode=args.mode,
            symbols=args.symbols,
            lookback_days=args.lookback_days,
            concurrency=args.concurrency,
            sandbox_timeout=args.timeout,
            drafter_prompt_path=args.drafter_prompt,
            reviewer_prompt_path=args.reviewer_prompt,
            reviser_prompt_path=args.reviser_prompt,
            model=model,
            base_url=base_url,
            skip_upload=args.skip_upload,
            max_symbols=args.max_symbols if args.max_symbols > 0 else None,
        )
        if summary["results"]:
            failed = summary.get("failure_count", 0)
            if failed > 0:
                LOGGER.warning("%d symbols failed", failed)
                sys.exit(1)
    except PipelineError as exc:
        LOGGER.error("Pipeline error: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
