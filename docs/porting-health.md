# Ported ADRF endpoint: CRM health check

Source inspected read-only on 2026-10-14:

- `organizations/views.py`,
  `HealthCheck` class near line 1230.
- `organizations/urls.py`,
  `path("health/", HealthCheck.as_view())` near line 61.
- `myproject/urls.py`,
  `path("api/", include("organizations.urls"))` near line 23.

The runnable port is `examples/ported_health.py`. No CRM source file was
changed. Every source-level change in the port is:

1. `adrf.views.APIView` import becomes `asyncdjango.rest.APIView`.
2. `adrf.requests.Request` import becomes `asyncdjango.asgi.Request`.
3. DRF `Response` import becomes `asyncdjango.rest.Response`.
4. DRF `AllowAny` import becomes `asyncdjango.permissions.AllowAny`.
5. `status.HTTP_200_OK` becomes the equivalent integer `200`; the returned
   status code and JSON body stay unchanged.
6. The `swagger_auto_schema` decorator and `openapi.Response` example are not
   portable. `schema_tags = ("Health",)` retains the tag, but the response
   example is not represented by the current schema generator.
7. Django's nested `path()` registration becomes
   `application.add_route("/api/health/", ...)`; the public URL stays the same.
8. The existing `logger.info("Health checked...")` call and async `get`
   method body are otherwise unchanged.

This port demonstrates APIView/Response routing, not ORM compatibility. The
source app uses other Django and ADRF features not covered here, and the
database concurrency benchmark is a separate task. No tests were run in the
CRM checkout; only this package's port test exercises the port.
