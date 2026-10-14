"""Guard the source path against thread bridges and detect event-loop stalls."""

import ast
import asyncio
from pathlib import Path
import time
import unittest

from asyncdjango.diagnostics import EventLoopBlockDetector


class ThreadBridgeGuardTests(unittest.TestCase):
    def test_request_and_database_source_has_no_thread_offload(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "asyncdjango"
        forbidden = {"sync_to_async", "to_thread", "run_in_executor", "ThreadPoolExecutor"}
        violations = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden:
                    violations.append(f"{path.name}:{node.lineno}:{node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                    violations.append(f"{path.name}:{node.lineno}:{node.attr}")
                elif isinstance(node, ast.ImportFrom) and node.module == "asgiref.sync":
                    violations.append(f"{path.name}:{node.lineno}:asgiref.sync")
        self.assertEqual(violations, [])


class EventLoopBlockDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_flags_blocking_sleep(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Event loop blocked"):
            async with EventLoopBlockDetector(interval=0.005, threshold=0.02):
                time.sleep(0.06)

    async def test_allows_nonblocking_sleep(self) -> None:
        async with EventLoopBlockDetector(interval=0.005, threshold=0.05):
            await asyncio.sleep(0.02)
