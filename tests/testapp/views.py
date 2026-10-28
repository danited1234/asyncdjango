import asyncio
import threading
import time

from django.db import transaction
from django.http import Http404, JsonResponse
from django.template.response import SimpleTemplateResponse

from .models import Organization

events = []


async def organizations(request):
    names = [org.name async for org in Organization.objects.order_by("name")]
    return JsonResponse({
        "names": names,
        "thread": threading.get_ident(),
        "middleware_thread": getattr(request, "middleware_thread", None),
        "org_count": getattr(request, "org_count", None),
    })


async def create_then_fail(request):
    await Organization.objects.acreate(name=request.GET["name"])
    if request.GET.get("fail"):
        raise RuntimeError("boom")
    return JsonResponse({"ok": True})


async def missing(request):
    raise Http404("nope")


async def slow_write(request):
    try:
        async with transaction.atomic():
            await Organization.objects.acreate(name="slow")
            events.append("created")
            await Organization.objects.extra(select={"s": "pg_sleep(5)"}).afirst()
    except asyncio.CancelledError:
        events.append("cancelled")
        raise
    return JsonResponse({"ok": True})


async def blocking(request):
    time.sleep(float(request.GET.get("seconds", "0.3")))  # deliberately blocks the loop
    return JsonResponse({"ok": True})


class LazyCountTemplate(SimpleTemplateResponse):
    """A deferred-render response whose rendering queries the database."""

    def __init__(self):
        super().__init__(template=None)

    @property
    def rendered_content(self):
        return str(Organization.objects.count())


async def deferred(request):
    return LazyCountTemplate()
