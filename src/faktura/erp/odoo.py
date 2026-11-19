"""Minimal client for Odoo's JSON-2 external API (Odoo 19+).

    POST /json/2/<model>/<method>
    Authorization: bearer <api key>
    body: {"ids": [...], "context": {...}, <named method arguments>}

Each call is its own database transaction on the Odoo side (commit on success,
rollback on error); several calls cannot be grouped into one transaction. Reads are
therefore safe to retry; writes are not, and callers must make them idempotent.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

RETRY_STATUS = {502, 503, 504}


class OdooError(Exception):
    """An error response from Odoo, with its exception name and message."""

    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"{status} {name}: {message}")
        self.status = status
        self.name = name
        self.message = message


class OdooClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        db: str | None = None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Authorization": f"bearer {api_key}", "User-Agent": "faktura-erp-client"}
        if db:
            headers["X-Odoo-Database"] = db
        self._http = httpx.Client(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
        )

    @classmethod
    def from_env(cls) -> OdooClient:
        return cls(
            url=os.environ["ODOO_URL"],
            api_key=os.environ["ODOO_API_KEY"],
            db=os.environ.get("ODOO_DB"),
        )

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        context: dict[str, Any] | None = None,
        retry: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Call `model.method`. Set `retry` only for calls that are safe to repeat."""
        body: dict[str, Any] = dict(kwargs)
        if ids is not None:
            body["ids"] = ids
        if context:
            body["context"] = context
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                r = self._http.post(f"/json/2/{model}/{method}", json=body)
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.5 * 2**attempt)
                continue
            if r.status_code in RETRY_STATUS and attempt + 1 < attempts:
                time.sleep(0.5 * 2**attempt)
                continue
            if r.is_success:
                return r.json()
            raise _error(r)
        raise AssertionError("unreachable")

    def search_read(
        self, model: str, domain: list, fields: list[str], order: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"domain": domain, "fields": fields}
        if order:
            kwargs["order"] = order
        return self.call(model, "search_read", retry=True, **kwargs)

    def close(self) -> None:
        self._http.close()


def _error(r: httpx.Response) -> OdooError:
    try:
        data = r.json()
        return OdooError(r.status_code, data.get("name", ""), data.get("message", r.text))
    except ValueError:
        return OdooError(r.status_code, "", r.text[:500])
