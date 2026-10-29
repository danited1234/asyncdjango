"""ASYNCDJANGO["CONNECTION_PER_REQUEST"]: one pooled connection per request."""

from django.conf import settings
from django.test import override_settings

from .test_asgi import HandlerTests


class ConnectionPerRequestTests(HandlerTests):
    """Every handler test, with one connection per request, plus its own."""

    async def asyncSetUp(self):
        self.per_request = override_settings(
            ASYNCDJANGO={**settings.ASYNCDJANGO, "CONNECTION_PER_REQUEST": True})
        self.per_request.enable()
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.per_request.disable()

    async def test_orm_calls_share_one_connection(self):
        response = await self.client().get("/backend-pids/")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(len(set(body["pids"])), 1)
        # Child tasks still get connections of their own.
        self.assertTrue(set(body["gathered"]).isdisjoint(body["pids"]))
        await self.assert_pool_idle()

    async def test_child_task_inside_a_transaction_is_still_refused(self):
        response = await self.client().get("/child-task-in-atomic/")
        self.assertEqual(response.json(), {"refused": True})
        await self.assert_pool_idle()
