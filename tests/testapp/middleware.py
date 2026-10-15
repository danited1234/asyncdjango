import threading

from .models import Organization


class SyncCountingMiddleware:
    """A synchronous-only middleware that queries the database."""

    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.middleware_thread = threading.get_ident()
        request.org_count = Organization.objects.count()
        response = self.get_response(request)
        response["X-Org-Count"] = str(request.org_count)
        return response


# --- Async middleware in the styles Django supports -------------------------

import asyncio  # noqa: E402

from asgiref.sync import iscoroutinefunction, markcoroutinefunction  # noqa: E402
from django.http import JsonResponse  # noqa: E402
from django.utils.decorators import async_only_middleware  # noqa: E402
from django.utils.deprecation import MiddlewareMixin  # noqa: E402


@async_only_middleware
def async_function_middleware(get_response):
    """Function-style async middleware that awaits the ORM and other I/O."""

    async def middleware(request):
        request.org_names = [o.name async for o in Organization.objects.order_by("name")]
        await asyncio.sleep(0)  # stands in for an await on httpx / redis.asyncio
        request.middleware_thread = threading.get_ident()
        response = await get_response(request)
        response["X-Async-Function"] = ",".join(request.org_names)
        return response

    return middleware


class DualModeMiddleware:
    """Class-style middleware with sync and async paths (the CRM's style)."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self.get_response):
            return self.__acall__(request)
        response = self.get_response(request)
        response["X-Dual-Mode"] = "sync"
        return response

    async def __acall__(self, request):
        response = await self.get_response(request)
        response["X-Dual-Mode"] = "async"
        return response


class MixinHeaderMiddleware(MiddlewareMixin):
    """A project MiddlewareMixin subclass with a query in process_request."""

    def process_request(self, request):
        request.mixin_count = Organization.objects.count()
        request.mixin_thread = threading.get_ident()

    def process_response(self, request, response):
        response["X-Mixin-Count"] = str(request.mixin_count)
        return response


class AsyncExceptionMiddleware(MiddlewareMixin):
    """An async process_exception() hook that queries."""

    async def process_exception(self, request, exception):
        if isinstance(exception, RuntimeError):
            total = await Organization.objects.acount()
            return JsonResponse({"handled": str(exception), "organizations": total},
                                status=418)
        return None
