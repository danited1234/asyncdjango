"""ADRF-compatible async Django REST Framework views and serializers.

The classes keep ADRF's public API (``await serializer.adata``,
``await self.aget_object()``, ``perform_acreate()``, ...) so porting is an
import change. Where ADRF moves work to a thread with ``sync_to_async``, these
run DRF's synchronous hooks (authentication, permissions, throttles, filter
backends, paginators, serializer fields and validators) natively on the event
loop, and await async hooks (``async def has_permission`` etc.) directly.

Synchronous hooks therefore must not block on I/O other than the database.
"""
