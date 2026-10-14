"""Port of a CRM back end's small ADRF GET /api/health/ endpoint."""

import logging

from asyncdjango.asgi import ASGIApplication, Request
from asyncdjango.permissions import AllowAny
from asyncdjango.rest import APIView, Response

logger = logging.getLogger(__name__)


class HealthCheck(APIView):
    permission_classes = [AllowAny]
    schema_tags = ("Health",)

    async def get(self, request: Request) -> Response:
        logger.info("Health checked...")
        return Response({"status": "ok"}, status=200)


def register_health(application: ASGIApplication) -> None:
    application.add_route("/api/health/", HealthCheck.as_view(), methods=("GET",))
