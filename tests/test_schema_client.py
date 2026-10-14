"""Schema and in-process ASGI client contracts."""

import asyncio
import unittest

from asyncdjango.asgi import ASGIApplication, JSONResponse, StreamingResponse
from asyncdjango.db import models
from asyncdjango.rest import ModelViewSet, Router
from asyncdjango.schema import generate_openapi
from asyncdjango.serializers import ModelSerializer
from asyncdjango.testclient import AsyncTestClient


class Product(models.Model):
    name = models.CharField(max_length=30)
    active = models.BooleanField(default=True)


class ProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "active")


class ProductViewSet(ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer


class FakeDatabase:
    def __init__(self):
        self.events = []

    async def connect(self):
        self.events.append("connect")

    async def close(self):
        self.events.append("close")


class SchemaTests(unittest.TestCase):
    def test_model_routes_have_request_response_and_parameter_schemas(self):
        app = ASGIApplication(FakeDatabase())
        Router(app).register("products", ProductViewSet)
        schema = generate_openapi(app, title="Products", version="1")
        self.assertEqual(schema["openapi"], "3.1.0")
        self.assertEqual(schema["info"], {"title": "Products", "version": "1"})
        collection = schema["paths"]["/products"]
        self.assertEqual(collection["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"], "array")
        self.assertEqual(collection["post"]["responses"]["201"]["description"], "Success")
        self.assertNotIn("id", collection["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"])
        detail = schema["paths"]["/products/{pk}"]
        self.assertEqual(detail["get"]["parameters"][0]["schema"], {"type": "integer", "readOnly": True})
        self.assertIn("204", detail["delete"]["responses"])
        fields = schema["components"]["schemas"]["ProductSerializer"]["properties"]
        self.assertEqual(fields["name"], {"type": "string", "maxLength": 30})
        self.assertEqual(fields["active"], {"type": "boolean"})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_json_and_streaming_round_trip(self):
        database = FakeDatabase()
        app = ASGIApplication(database)

        async def echo(request):
            return JSONResponse(await request.json(), 201)

        async def upload(request):
            async def output():
                async for chunk in request.stream():
                    yield chunk.upper()
            return StreamingResponse(output())

        app.add_route("/echo", echo, methods=("POST",))
        app.add_route("/upload", upload, methods=("POST",), stream_request=True)

        async def chunks():
            yield b"one"
            await asyncio.sleep(0)
            yield b"two"

        async with AsyncTestClient(app) as client:
            response = await client.post("/echo", json_data={"ok": True})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json(), {"ok": True})
            response = await client.request("POST", "/upload", body=chunks())
            self.assertEqual(response.body, b"ONETWO")
            response = await client.post("/echo", json_data=None)
            self.assertIsNone(response.json())
        self.assertEqual(database.events, ["connect", "close"])
