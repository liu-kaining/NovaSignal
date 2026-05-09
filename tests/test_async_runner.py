import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.orchestrator.async_runner import AgentResult, AgentRunError, invoke_agent


class InvokeAgentTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _mock_process(self, stdout=b"", stderr=b"", returncode=0):
        process = AsyncMock()
        process.communicate = AsyncMock(return_value=(stdout, stderr))
        process.returncode = returncode
        process.pid = 12345
        process.kill = MagicMock()
        process.wait = AsyncMock()
        return process

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_successful_invocation(self, mock_exec):
        process = self._mock_process(stdout=b"Agent output", returncode=0)
        mock_exec.return_value = process

        result = self._run(invoke_agent(
            "Analyze AAPL",
            working_dir="/tmp/sandbox",
            retry_attempts=1,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        ))

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.success)
        self.assertEqual(result.stdout, "Agent output")
        self.assertEqual(result.return_code, 0)
        self.assertFalse(result.timed_out)

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_timeout_kills_process(self, mock_exec):
        process = AsyncMock()
        process.pid = 99999
        process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        process.kill = MagicMock()
        process.wait = AsyncMock()
        mock_exec.return_value = process

        with self.assertRaises(AgentRunError) as ctx:
            self._run(invoke_agent(
                "Analyze",
                working_dir="/tmp",
                timeout_seconds=1,
                retry_attempts=1,
                retry_min_wait_seconds=0,
                retry_max_wait_seconds=0,
            ))
        self.assertIn("timed out", str(ctx.exception).lower())
        process.kill.assert_called_once()

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_non_retryable_exit_code_raises_immediately(self, mock_exec):
        process = self._mock_process(stderr=b"auth error", returncode=127)
        mock_exec.return_value = process

        with self.assertRaises(AgentRunError):
            self._run(invoke_agent(
                "Analyze",
                working_dir="/tmp",
                retry_attempts=3,
                retry_min_wait_seconds=0,
                retry_max_wait_seconds=0,
            ))
        self.assertEqual(mock_exec.call_count, 1)

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_retryable_exit_code_retries(self, mock_exec):
        fail_process = self._mock_process(stderr=b"transient", returncode=1)
        success_process = self._mock_process(stdout=b"ok", returncode=0)
        mock_exec.side_effect = [fail_process, success_process]

        result = self._run(invoke_agent(
            "Analyze",
            working_dir="/tmp",
            retry_attempts=2,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        ))
        self.assertTrue(result.success)
        self.assertEqual(mock_exec.call_count, 2)

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_os_error_raises_agent_run_error(self, mock_exec):
        mock_exec.side_effect = OSError("command not found")

        with self.assertRaises(AgentRunError) as ctx:
            self._run(invoke_agent(
                "Analyze",
                working_dir="/tmp",
                retry_attempts=1,
                retry_min_wait_seconds=0,
                retry_max_wait_seconds=0,
            ))
        self.assertIn("Failed to start", str(ctx.exception))

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_env_vars_passed_to_subprocess(self, mock_exec):
        process = self._mock_process(returncode=0)
        mock_exec.return_value = process

        self._run(invoke_agent(
            "Analyze",
            working_dir="/tmp",
            env_vars={"ANTHROPIC_API_KEY": "test-key"},
            retry_attempts=1,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        ))

        call_kwargs = mock_exec.call_args[1]
        self.assertEqual(call_kwargs["env"]["ANTHROPIC_API_KEY"], "test-key")

    @patch("src.orchestrator.async_runner.asyncio.create_subprocess_exec")
    def test_command_includes_prompt(self, mock_exec):
        process = self._mock_process(returncode=0)
        mock_exec.return_value = process

        self._run(invoke_agent(
            "My prompt text",
            working_dir="/tmp/work",
            agent_command="claude --print",
            retry_attempts=1,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        ))

        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args, ("claude", "--print", "-p", "My prompt text"))
        self.assertEqual(mock_exec.call_args[1]["cwd"], "/tmp/work")

    def test_agent_result_success_property(self):
        self.assertTrue(AgentResult("out", "", 0).success)
        self.assertFalse(AgentResult("", "err", 1).success)
        self.assertFalse(AgentResult("", "", 0, timed_out=True).success)


if __name__ == "__main__":
    unittest.main()
