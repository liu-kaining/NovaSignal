"""Async subprocess runner for Claude agent invocation."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)


class AgentRunError(RuntimeError):
    """Raised when agent invocation fails."""


@dataclass
class AgentResult:
    """Result of an agent subprocess invocation."""

    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.timed_out


async def invoke_agent(
    prompt: str,
    *,
    working_dir: str | Path,
    timeout_seconds: float = 300,
    agent_command: str = "claude --print --dangerously-skip-permissions",
    env_vars: dict[str, str] | None = None,
    model: str | None = None,
    retry_attempts: int = 3,
    retry_min_wait_seconds: float = 5,
    retry_max_wait_seconds: float = 30,
) -> AgentResult:
    """Invoke the Claude agent via subprocess with timeout and retry.

    Runs the agent command in the specified working directory, passing the
    prompt via -p flag. Retries on transient failures (non-zero exit codes
    that are not permission/auth errors).
    """
    cmd_parts = agent_command.split()
    if model:
        cmd_parts += ["--model", model]
    cmd_parts += ["-p", prompt]
    env = _build_env(env_vars)
    work_dir = str(working_dir)

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(retry_attempts),
        wait=wait_exponential(min=retry_min_wait_seconds, max=retry_max_wait_seconds),
        retry=retry_if_exception_type(_RetryableAgentError),
        reraise=True,
        before_sleep=_log_async_retry,
    ):
        with attempt:
            result = await _run_subprocess(cmd_parts, work_dir, env, timeout_seconds)
            if not result.success:
                if result.timed_out:
                    raise AgentRunError(
                        f"Agent timed out after {timeout_seconds}s"
                    )
                if _is_retryable_exit(result):
                    raise _RetryableAgentError(
                        f"Agent exited with code {result.return_code}, retrying"
                    )
                raise AgentRunError(
                    f"Agent failed with exit code {result.return_code}: "
                    f"{result.stderr[:500]}"
                )
            return result

    raise AgentRunError("Agent invocation failed without returning a result")


async def _run_subprocess(
    cmd: list[str],
    working_dir: str,
    env: dict[str, str],
    timeout_seconds: float,
) -> AgentResult:
    """Execute the subprocess with timeout handling."""
    LOGGER.info("Running agent command in %s", working_dir)
    LOGGER.debug("Command: %s", " ".join(cmd[:3]) + " ...")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
    except OSError as exc:
        raise AgentRunError(f"Failed to start agent process: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        LOGGER.warning("Agent process timed out, killing PID %s", process.pid)
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        return AgentResult(
            stdout="",
            stderr="Process killed due to timeout",
            return_code=-1,
            timed_out=True,
        )

    return AgentResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        return_code=process.returncode or 0,
    )


def _build_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Build subprocess environment from current env plus extras."""
    env = dict(os.environ)
    if extra:
        env.update(extra)
    return env


def _is_retryable_exit(result: AgentResult) -> bool:
    """Determine if a failed agent run should be retried."""
    if result.timed_out:
        return True
    if result.return_code in (1, 2):
        return True
    return False


class _RetryableAgentError(AgentRunError):
    """Internal marker for retryable agent failures."""


def _log_async_retry(retry_state: Any) -> None:
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    LOGGER.warning(
        "Retrying agent invocation after attempt %s: %s",
        retry_state.attempt_number,
        exception,
    )
