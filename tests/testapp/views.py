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


def _backend_pid():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


async def backend_pids(request):
    from asyncdjango import gather, sync_to_native

    pids = [await sync_to_native(_backend_pid)() for _ in range(3)]
    await Organization.objects.acount()
    pids.append(await sync_to_native(_backend_pid)())
    gathered = await gather(sync_to_native(_backend_pid)(), sync_to_native(_backend_pid)())
    return JsonResponse({"pids": pids, "gathered": gathered})


async def child_task_in_atomic(request):
    from asyncdjango.exceptions import CrossTaskConnectionError

    async with transaction.atomic():
        await Organization.objects.acreate(name="in-atomic")
        try:
            await asyncio.create_task(Organization.objects.acount())
        except CrossTaskConnectionError:
            return JsonResponse({"refused": True})
    return JsonResponse({"refused": False})


class LazyCountTemplate(SimpleTemplateResponse):
    """A deferred-render response whose rendering queries the database."""

    def __init__(self):
        super().__init__(template=None)

    @property
    def rendered_content(self):
        return str(Organization.objects.count())


async def deferred(request):
    return LazyCountTemplate()
