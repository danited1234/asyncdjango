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
