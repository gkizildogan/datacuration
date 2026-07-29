from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from aviation_data.models import MediaWikiApiConfig


class MediaWikiApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiResponse:
    data: Mapping[str, Any]
    request_url: str
    response_headers: dict[str, str]
    redirect_chain: list[str]


@dataclass(frozen=True)
class MediaWikiPage:
    page_id: int
    title: str
    revision_id: int
    revision_timestamp: str
    canonical_url: str

    @property
    def permanent_url(self) -> str:
        parsed = urlsplit(self.canonical_url)
        query = urlencode({"title": self.title.replace(" ", "_"), "oldid": self.revision_id})
        return urlunsplit((parsed.scheme, parsed.netloc, "/w/index.php", query, ""))

    @property
    def history_url(self) -> str:
        parsed = urlsplit(self.canonical_url)
        query = urlencode({"title": self.title.replace(" ", "_"), "action": "history"})
        return urlunsplit((parsed.scheme, parsed.netloc, "/w/index.php", query, ""))


@dataclass(frozen=True)
class RenderedMediaWikiPage:
    page: MediaWikiPage
    html: bytes
    api_response: ApiResponse


ApiRequest = Callable[[str, dict[str, str]], Awaitable[ApiResponse]]


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _page_from_query(raw: Mapping[str, Any]) -> MediaWikiPage | None:
    if raw.get("missing") is not None or "pageid" not in raw:
        return None
    revisions = raw.get("revisions", [])
    if not revisions:
        return None
    revision = revisions[0]
    try:
        return MediaWikiPage(
            page_id=int(raw["pageid"]),
            title=str(raw["title"]),
            revision_id=int(revision["revid"]),
            revision_timestamp=str(revision["timestamp"]),
            canonical_url=str(raw["fullurl"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaWikiApiError(f"incomplete page revision metadata: {raw!r}") from exc


def _collect_pages(payload: Mapping[str, Any], pages: dict[int, MediaWikiPage]) -> None:
    query = payload.get("query")
    if not isinstance(query, Mapping):
        raise MediaWikiApiError("MediaWiki discovery response has no query object")
    raw_pages = query.get("pages", [])
    if not isinstance(raw_pages, list):
        raise MediaWikiApiError("MediaWiki discovery response has invalid pages")
    for raw in raw_pages:
        if not isinstance(raw, Mapping):
            continue
        page = _page_from_query(raw)
        if page is not None:
            pages[page.page_id] = page


def _common_query(maxlag: int) -> dict[str, str]:
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "maxlag": str(maxlag),
        "prop": "info|revisions",
        "inprop": "url",
        "rvprop": "ids|timestamp",
        "rvslots": "main",
        "redirects": "1",
    }


async def discover_pages(
    request: ApiRequest,
    endpoint: str,
    config: MediaWikiApiConfig,
) -> list[MediaWikiPage]:
    pages: dict[int, MediaWikiPage] = {}

    for title_batch in _chunks(config.page_titles, config.batch_size):
        params = {
            **_common_query(config.maxlag),
            "titles": "|".join(title_batch),
        }
        result = await request(endpoint, params)
        _collect_pages(result.data, pages)

    for category in config.category_titles:
        continuation: str | None = None
        while len(pages) < config.max_pages:
            remaining = config.max_pages - len(pages)
            params = {
                **_common_query(config.maxlag),
                "generator": "categorymembers",
                "gcmtitle": category,
                "gcmnamespace": "0",
                "gcmtype": "page",
                "gcmlimit": str(min(500, remaining)),
            }
            if continuation:
                params["gcmcontinue"] = continuation
            result = await request(endpoint, params)
            _collect_pages(result.data, pages)
            raw_continue = result.data.get("continue", {})
            continuation = (
                str(raw_continue["gcmcontinue"])
                if isinstance(raw_continue, Mapping) and "gcmcontinue" in raw_continue
                else None
            )
            if not continuation:
                break

    ordered = sorted(pages.values(), key=lambda page: (page.title.casefold(), page.page_id))
    return ordered[: config.max_pages]


async def render_page(
    request: ApiRequest,
    endpoint: str,
    config: MediaWikiApiConfig,
    page: MediaWikiPage,
) -> RenderedMediaWikiPage:
    result = await request(
        endpoint,
        {
            "action": "parse",
            "format": "json",
            "formatversion": "2",
            "maxlag": str(config.maxlag),
            "oldid": str(page.revision_id),
            "prop": "text",
            "disableeditsection": "1",
            "disabletoc": "1",
        },
    )
    parsed = result.data.get("parse")
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("text"), str):
        raise MediaWikiApiError(f"revision {page.revision_id} has no rendered HTML")
    return RenderedMediaWikiPage(
        page=page,
        html=str(parsed["text"]).encode("utf-8"),
        api_response=result,
    )
