"""Authentication, permission, and throttle behavior on async views."""

import asyncio
import unittest
from dataclasses import dataclass

from asyncdjango.asgi import ASGIApplication, JSONResponse, Request
from asyncdjango.authentication import AuthenticationFailed, TokenAuthentication
from asyncdjango.permissions import BasePermission, IsAuthenticated
from asyncdjango.rest import APIView
from asyncdjango.testclient import AsyncTestClient
from asyncdjango.throttling import SimpleRateThrottle


@dataclass
class User:
    pk: int
    is_authenticated: bool = True


class StaticTokenAuthentication(TokenAuthentication):
    async def authenticate_credentials(self, key):
        await asyncio.sleep(0)
        if key != "good-token":
            raise AuthenticationFailed("Invalid token")
        return User(7), key


class NamedPermission(BasePermission):
    async def has_permission(self, request, view):
        return request.user.pk == 7


class TwoPerMinute(SimpleRateThrottle):
    rate = "2/min"


class ProtectedView(APIView):
    authentication_classes = (StaticTokenAuthentication,)
    permission_classes = (IsAuthenticated, NamedPermission)
    throttle_classes = (TwoPerMinute,)

    async def get(self, request):
        return JSONResponse({"user": request.user.pk, "auth": request.auth})


class FakeDatabase:
    async def connect(self):
        pass

    async def close(self):
        pass


class RestPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        TwoPerMinute._history.clear()
        self.app = ASGIApplication(FakeDatabase())
        self.app.add_route("/protected", ProtectedView.as_view())

    async def test_authentication_permission_and_throttle_order(self):
        async with AsyncTestClient(self.app) as client:
            missing = await client.get("/protected")
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.headers[b"www-authenticate"], b"Token")
            bad = await client.get("/protected", headers={
                "authorization": "Token bad-token"
            })
            self.assertEqual(bad.status_code, 401)
            valid = {"authorization": "Token good-token"}
            one = await client.get("/protected", headers=valid)
            two = await client.get("/protected", headers=valid)
            third = await client.get("/protected", headers=valid)
            self.assertEqual(one.json(), {"user": 7, "auth": "good-token"})
            self.assertEqual(two.status_code, 200)
            self.assertEqual(third.status_code, 429)
            self.assertIn(b"retry-after", third.headers)

    async def test_malformed_token_and_duplicate_authorization_headers(self):
        async with AsyncTestClient(self.app) as client:
            malformed = await client.get("/protected", headers={
                "authorization": "Token two parts here"
            })
            self.assertEqual(malformed.status_code, 401)
        request = Request({"type": "http", "method": "GET", "path": "/",
                           "headers": [(b"authorization", b"Token good-token"),
                                       (b"authorization", b"Token good-token")]}, b"")
        with self.assertRaises(AuthenticationFailed):
            await StaticTokenAuthentication().authenticate(request)
