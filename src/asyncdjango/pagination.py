"""Native async page-number pagination for model querysets."""

from __future__ import annotations

from urllib.parse import urlencode

from .asgi import JSONResponse, Request
from .db.models import QuerySet


class PaginationError(ValueError):
    pass


class PageNumberPagination:
    page_size = 20
    page_query_param = "page"
    page_size_query_param = "page_size"
    max_page_size = 100

    async def paginate_queryset(self, queryset: QuerySet, request: Request):
        params = request.query_params
        try:
            page = int(params.get(self.page_query_param, "1"))
            size = int(params.get(self.page_size_query_param, str(self.page_size)))
        except ValueError as exc:
            raise PaginationError("Invalid pagination parameters") from exc
        if page < 1 or size < 1 or size > self.max_page_size:
            raise PaginationError("Invalid pagination parameters")
        count = await queryset.count()
        start = (page - 1) * size
        results = await queryset[start:start + size].fetch_all()
        self.count = count
        self.page = page
        self.size = size
        self.request = request
        return results

    def _link(self, page: int) -> str:
        params = {**self.request.query_params, self.page_query_param: str(page),
                  self.page_size_query_param: str(self.size)}
        return f"{self.request.path}?{urlencode(params)}"

    def get_paginated_response(self, data) -> JSONResponse:
        return JSONResponse({
            "count": self.count,
            "next": self._link(self.page + 1) if self.page * self.size < self.count else None,
            "previous": self._link(self.page - 1) if self.page > 1 else None,
            "results": data,
        })
