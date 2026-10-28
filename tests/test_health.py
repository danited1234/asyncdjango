"""asyncdjango.views: the health and metrics endpoints."""

from django.test import override_settings

from asyncdjango.asgi import NativeASGIHandler
from asyncdjango.db.pool import get_pool
from asyncdjango.test import ASGIClient

from .test_orm import OffloadGuardedTestCase
from .testapp.models import Organization


class HealthTests(OffloadGuardedTestCase):
    def client(self):
        return ASGIClient(NativeASGIHandler())

    async def test_healthy(self):
        response = await self.client().get("/health/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["databases"], {"default": "ok"})  # "other" is SQLite
        self.assertEqual(body["pools"]["default"]["pool_max"], 8)
        self.assertIsNone(body["stalls"])

    async def test_unhealthy_when_every_connection_is_busy(self):
        # The 503 is logged from a thread on purpose (asyncdjango._threads).
        self.offload.forbid = False
        pool = await get_pool("default")
        held = [await pool.getconn() for _ in range(pool.max_size)]
        try:
            with self.assertLogs("asyncdjango.health", "WARNING"):
                response = await self.client().get("/health/quick/")
        finally:
            for connection in held:
                await pool.putconn(connection)
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["databases"], {"default": "PoolTimeout"})
        self.assertEqual(body["pools"]["default"]["pool_available"], 0)

    @override_settings(ASYNCDJANGO={"POOL": {"min_size": 1, "max_size": 8},
                                    "STALL_THRESHOLD": 0.5})
    async def test_prometheus_metrics(self):
        await Organization.objects.acreate(name="Acme")
        response = await self.client().get("/metrics/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        text = response.content.decode()
        self.assertIn("# TYPE asyncdjango_pool_max gauge", text)
        self.assertRegex(text, r'asyncdjango_pool_max\{worker="\d+",alias="default"\} 8')
        self.assertIn("# TYPE asyncdjango_pool_requests_num_total counter", text)
        self.assertIn("# TYPE asyncdjango_pool_requests_waiting gauge", text)
        self.assertRegex(text, r'asyncdjango_loop_stalls_total\{worker="\d+"\} 0')
