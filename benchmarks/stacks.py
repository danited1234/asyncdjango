"""The stacks under comparison. Each runs the same endpoints in its own process."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Stack:
    key: str
    label: str
    urlconf: str
    interface: str  # "asgi" or "wsgi"
    native: bool  # asyncdjango installed


STACKS = {stack.key: stack for stack in [
    Stack("asyncdjango-drf", "asyncdjango.rest_framework", "benchmarks.views_async_drf",
          "asgi", True),
    Stack("adrf", "ADRF", "benchmarks.views_async_drf", "asgi", False),
    Stack("drf", "DRF (sync, WSGI)", "benchmarks.views_drf", "wsgi", False),
    Stack("asyncdjango", "asyncdjango + Django views", "benchmarks.views_django",
          "asgi", True),
    Stack("django", "Django async views", "benchmarks.views_django", "asgi", False),
]}
