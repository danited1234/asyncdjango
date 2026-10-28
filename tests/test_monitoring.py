"""The event-loop stall watchdog."""

import asyncio

from django.test import SimpleTestCase, override_settings

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.monitoring import stall_stats, watch_event_loop
from asyncdjango.test import ASGIClient


class StallWatchdogTests(SimpleTestCase):
    @override_settings(ASYNCDJANGO={"STALL_THRESHOLD": 0.05})
    async def test_logs_blocking_view_with_request_and_stack(self):
        client = ASGIClient(NativeASGIHandler())
        with self.assertLogs("asyncdjango.stalls", "WARNING") as logs:
            response = await client.get("/blocking/", query_string="seconds=0.3")
            await asyncio.sleep(0.05)
        self.assertEqual(response.status_code, 200)
        message = logs.output[0]
        self.assertIn("during GET /blocking/", message)
        self.assertIn("in blocking", message)
        self.assertIn("time.sleep", message)
        stats = stall_stats()
        self.assertGreaterEqual(stats["stalls"], 1)
        self.assertGreaterEqual(stats["longest"], 0.25)

    async def test_quiet_when_nothing_blocks(self):
        watchdog = watch_event_loop(0.2)
        with self.assertNoLogs("asyncdjango.stalls"):
            for _ in range(20):
                await asyncio.sleep(0.02)
        self.assertEqual(watchdog.stats()["stalls"], 0)

    async def test_off_by_default(self):
        self.assertIsNone(watch_event_loop())
        self.assertIsNone(stall_stats())
        await ASGIClient(NativeASGIHandler()).get("/missing/")
        self.assertIsNone(stall_stats())
