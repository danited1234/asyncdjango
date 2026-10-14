"""Response and route parity for a small source ADRF endpoint."""

import unittest

from asyncdjango.asgi import ASGIApplication
from asyncdjango.schema import generate_openapi
from asyncdjango.testclient import AsyncTestClient
from examples.ported_health import register_health


class FakeDatabase:
    async def connect(self):
        pass

    async def close(self):
        pass


class PortedHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_preserves_route_status_body_and_tag(self):
        app = ASGIApplication(FakeDatabase())
        register_health(app)
        async with AsyncTestClient(app) as client:
            response = await client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        schema = generate_openapi(app)
        self.assertEqual(schema["paths"]["/api/health/"]["get"]["tags"],
                         ["Health"])
