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
from typing import Any

from src.fetchers.fmp_client import FMPClient, configure_logging
from src.orchestrator.async_runner import AgentRunError, invoke_agent
from src.orchestrator.sandbox_manager import SandboxError, SandboxManager
from src.storage.r2_client import R2Client, R2StorageError

LOGGER = logging.getLogger(__name__)

PIPELINE_MODES = ("discovery-only", "production", "dev")


class PipelineError(RuntimeError):
    """Raised when the pipeline encounters a fatal error."""


def run_pipeline(
    mode: str = "production",
    *,
    symbols: list[str] | None = None,
    lookback_days: int = 7,
    concurrency: int = 4,
    sandbox_timeout: float = 300,
    prompt_path: str | Path = "prompts/ipo_v1_template.md",
    skip_upload: bool = False,
) -> dict[str, Any]:
    """Execute the NovaSignal analysis pipeline.

    Modes:
        discovery-only: Fetch IPO calendar and output discovered symbols.
        production: Full pipeline (discover -> sandbox -> agent -> upload).
        dev: Like production but skips R2 upload and prints results.
    """
    if mode not in PIPELINE_MODES:
        raise PipelineError(f"Invalid mode: {mode}. Must be one of {PIPELINE_MODES}")

    LOGGER.info("Starting pipeline in '%s' mode", mode)

    # Phase 1: Discovery
    fmp = _create_fmp_client()
    discovered = _discover_symbols(fmp, lookback_days, symbols)

    # Production mode: deduplicate against existing R2 reports (idempotency)
    # to prevent re-analyzing and burning tokens on already processed symbols
    should_upload = mode == "production"
    if should_upload and symbols is None:
        try:
            r2_check = _create_r2_client()
            discovered = _filter_already_processed(r2_check, discovered)
        except Exception as exc:
            LOGGER.warning("Failed to check existing reports for deduplication: %s", exc)

    symbol_names = [e["symbol"] for e in discovered]

    if mode == "discovery-only":
        LOGGER.info("Discovery-only mode: found %d symbols", len(discovered))
        _persist_discovery(discovered)
        return {"mode": mode, "symbols": symbol_names, "results": []}

    # Phase 2: Load prompt template
    prompt_content = _load_prompt(prompt_path)

    # Phase 3: Execute agent for each symbol
    should_upload = mode == "production" and not skip_upload
    r2 = _create_r2_client() if should_upload else None
    sandbox_mgr = SandboxManager()

    results = asyncio.run(
        _run_all_agents(
            discovery_entries=discovered,
            fmp=fmp,
            prompt_content=prompt_content,
            sandbox_mgr=sandbox_mgr,
            r2=r2,
            concurrency=concurrency,
            timeout=sandbox_timeout,
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
    prompt_content: str,
    sandbox_mgr: SandboxManager,
    r2: R2Client | None,
    concurrency: int,
    timeout: float,
) -> list[dict[str, Any]]:
    """Run agent invocations with bounded concurrency."""
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _run_single_agent(
            entry=entry,
            fmp=fmp,
            prompt_content=prompt_content,
            sandbox_mgr=sandbox_mgr,
            r2=r2,
            timeout=timeout,
            semaphore=semaphore,
        )
        for entry in discovery_entries
    ]
    return await asyncio.gather(*tasks)


async def _run_single_agent(
    *,
    entry: dict[str, Any],
    fmp: FMPClient,
    prompt_content: str,
    sandbox_mgr: SandboxManager,
    r2: R2Client | None,
    timeout: float,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Execute a single agent run within a sandbox."""
    symbol = entry["symbol"]
    async with semaphore:
        context = None
        try:
            raw_data = _build_raw_data(entry, fmp)
            context = sandbox_mgr.create(symbol, raw_data, prompt_content)

            agent_instruction = (
                "Read prompt.md and raw_data.json in your working directory. "
                "Follow the instructions in prompt.md to analyze the data in raw_data.json. "
                "Produce the output files as specified."
            )
            result = await invoke_agent(
                agent_instruction,
                working_dir=context.sandbox_dir,
                timeout_seconds=timeout,
                env_vars={"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "")},
            )

            outputs = sandbox_mgr.extract_results(context)

            if r2 and outputs.get("report"):
                r2.upload_report(symbol, outputs["report"])
            if r2 and outputs.get("metrics"):
                r2.upload_metrics(symbol, outputs["metrics"])

            return {
                "symbol": symbol,
                "success": True,
                "report": outputs.get("report"),
                "metrics": outputs.get("metrics"),
            }

        except (AgentRunError, SandboxError) as exc:
            LOGGER.error("Agent run failed for %s: %s", symbol, exc)
            return {"symbol": symbol, "success": False, "error": str(exc)}
        except R2StorageError as exc:
            LOGGER.error("Upload failed for %s: %s", symbol, exc)
            return {"symbol": symbol, "success": False, "error": f"Upload: {exc}"}
        finally:
            if context:
                sandbox_mgr.cleanup(context)


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


def _build_raw_data(entry: dict[str, Any], fmp: FMPClient) -> dict[str, Any]:
    """Build comprehensive raw_data for the agent sandbox.

    Enriches the discovery entry with fundraising data from FMP.
    """
    symbol = entry["symbol"]
    raw_data: dict[str, Any] = {
        "symbol": symbol,
        "timestamp": date.today().isoformat(),
        "ipo_data": {
            k: v for k, v in entry.items() if k != "symbol"
        },
    }

    # Attempt to fetch fundraising data (non-fatal if unavailable)
    cik = entry.get("cik")
    if cik:
        try:
            raw_data["fundraising"] = fmp.get_fundraising(cik)
        except Exception as exc:
            LOGGER.warning("Could not fetch fundraising for %s: %s", symbol, exc)
            raw_data["fundraising"] = []
    else:
        raw_data["fundraising"] = []

    return raw_data


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
    """Create FMP client from environment."""
    return FMPClient()


def _create_r2_client() -> R2Client:
    """Create R2 client from environment."""
    return R2Client()


def _filter_already_processed(
    r2: R2Client, discovered: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Filter out symbols that already have reports in R2 (idempotency).

    Prevents burning tokens on re-analyzing the same stocks every day.
    Returns only the entries that haven't been processed yet.
    """
    LOGGER.info("Checking R2 for already processed symbols...")
    existing_keys = r2.list_objects("reports/")
    # Extract symbol names from keys like "reports/2026-01-01/AAPL_report.md"
    processed_symbols = {
        k.split('/')[-1].replace('_report.md', '')
        for k in existing_keys
    }
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


def main() -> None:
    """CLI entry point for run_pipeline."""
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
        default=4,
        help="Max concurrent agent invocations",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Sandbox timeout in seconds",
    )
    parser.add_argument(
        "--prompt",
        default="prompts/ipo_v1_template.md",
        help="Path to prompt template",
    )

    args = parser.parse_args()
    configure_logging()

    try:
        summary = run_pipeline(
            mode=args.mode,
            symbols=args.symbols,
            lookback_days=args.lookback_days,
            concurrency=args.concurrency,
            sandbox_timeout=args.timeout,
            prompt_path=args.prompt,
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
