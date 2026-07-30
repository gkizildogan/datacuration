from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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
    discovery_categories: tuple[str, ...] = ()
    explicit_title: bool = False

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
    categories: tuple[str, ...]
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


def _merge_page(
    pages: dict[int, MediaWikiPage],
    page: MediaWikiPage,
) -> None:
    previous = pages.get(page.page_id)
    if previous is None:
        pages[page.page_id] = page
        return
    pages[page.page_id] = replace(
        previous,
        discovery_categories=tuple(
            sorted(
                {*previous.discovery_categories, *page.discovery_categories},
                key=str.casefold,
            )
        ),
        explicit_title=previous.explicit_title or page.explicit_title,
    )


def _collect_pages(
    payload: Mapping[str, Any],
    pages: dict[int, MediaWikiPage],
    *,
    discovery_category: str | None = None,
    explicit_title: bool = False,
) -> None:
    query = payload.get("query")
    if query is None and payload.get("batchcomplete") is True:
        return
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
            _merge_page(
                pages,
                replace(
                    page,
                    discovery_categories=(
                        (discovery_category,) if discovery_category is not None else ()
                    ),
                    explicit_title=explicit_title,
                ),
            )


def _normalized_category_title(value: str) -> str:
    value = value.replace("_", " ").strip()
    for prefix in ("Category:", "Kategori:"):
        if value.casefold().startswith(prefix.casefold()):
            value = value[len(prefix) :]
            break
    return re.sub(r"\s+", " ", value).strip()


def _category_matches(
    categories: list[str] | tuple[str, ...],
    excluded_titles: list[str],
    excluded_patterns: list[str],
) -> list[str]:
    exact = {_normalized_category_title(value).casefold() for value in excluded_titles}
    matches = []
    for category in categories:
        normalized = _normalized_category_title(category)
        if normalized.casefold() in exact or any(
            re.search(pattern, normalized) for pattern in excluded_patterns
        ):
            matches.append(category)
    return sorted(set(matches), key=str.casefold)


def excluded_discovery_category_matches(
    categories: list[str] | tuple[str, ...],
    config: MediaWikiApiConfig,
) -> list[str]:
    return _category_matches(
        categories,
        config.excluded_category_titles,
        config.excluded_category_patterns,
    )


def excluded_page_category_matches(
    categories: list[str] | tuple[str, ...],
    config: MediaWikiApiConfig,
) -> list[str]:
    return _category_matches(
        categories,
        config.excluded_page_category_titles,
        config.excluded_page_category_patterns,
    )


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
    explicit_pages: dict[int, MediaWikiPage] = {}

    for title_batch in _chunks(config.page_titles, config.batch_size):
        params = {
            **_common_query(config.maxlag),
            "titles": "|".join(title_batch),
        }
        result = await request(endpoint, params)
        _collect_pages(result.data, explicit_pages, explicit_title=True)

    categories = [
        category
        for category in dict.fromkeys(config.category_titles)
        if not excluded_discovery_category_matches([category], config)
    ]
    if config.subcategory_depth == 1:
        discovered_subcategories: set[str] = set()
        for category in sorted(categories, key=str.casefold):
            continuation: str | None = None
            while len(discovered_subcategories) < config.max_subcategories:
                params = {
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "maxlag": str(config.maxlag),
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmnamespace": "14",
                    "cmtype": "subcat",
                    "cmlimit": str(
                        min(500, config.max_subcategories - len(discovered_subcategories))
                    ),
                    "cmsort": "sortkey",
                    "cmdir": "ascending",
                }
                if continuation:
                    params["cmcontinue"] = continuation
                result = await request(endpoint, params)
                query = result.data.get("query")
                members = query.get("categorymembers", []) if isinstance(query, Mapping) else []
                if not isinstance(members, list):
                    raise MediaWikiApiError(
                        "MediaWiki subcategory response has invalid categorymembers"
                    )
                candidates = [
                    str(member["title"])
                    for member in members
                    if isinstance(member, Mapping) and member.get("title")
                ]
                discovered_subcategories.update(
                    category
                    for category in candidates
                    if not excluded_discovery_category_matches([category], config)
                )
                raw_continue = result.data.get("continue", {})
                continuation = (
                    str(raw_continue["cmcontinue"])
                    if isinstance(raw_continue, Mapping) and "cmcontinue" in raw_continue
                    else None
                )
                if not continuation:
                    break
        categories.extend(
            sorted(discovered_subcategories, key=str.casefold)[: config.max_subcategories]
        )

    category_pages: dict[str, list[MediaWikiPage]] = {}
    for category in sorted(dict.fromkeys(categories), key=str.casefold):
        pages: dict[int, MediaWikiPage] = {}
        continuation: str | None = None
        while len(pages) < config.max_pages_per_category:
            remaining = config.max_pages_per_category - len(pages)
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
            _collect_pages(
                result.data,
                pages,
                discovery_category=category,
            )
            raw_continue = result.data.get("continue", {})
            continuation = (
                str(raw_continue["gcmcontinue"])
                if isinstance(raw_continue, Mapping) and "gcmcontinue" in raw_continue
                else None
            )
            if not continuation:
                break
        category_pages[category] = sorted(
            pages.values(),
            key=lambda page: (page.title.casefold(), page.page_id),
        )

    candidate_limit = config.max_candidate_pages or config.max_pages
    selected = dict(explicit_pages)
    global_pages = dict(explicit_pages)
    for rows in category_pages.values():
        for page in rows:
            _merge_page(global_pages, page)
    maximum_category_size = max((len(rows) for rows in category_pages.values()), default=0)
    for index in range(maximum_category_size):
        for category in sorted(category_pages, key=str.casefold):
            rows = category_pages[category]
            if index >= len(rows):
                continue
            page = global_pages[rows[index].page_id]
            _merge_page(selected, page)
            if len(selected) >= candidate_limit:
                break
        if len(selected) >= candidate_limit:
            break

    ordered = sorted(selected.values(), key=lambda page: (page.title.casefold(), page.page_id))
    return ordered[:candidate_limit]


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
            "prop": "text|categories",
            "disableeditsection": "1",
            "disabletoc": "1",
        },
    )
    parsed = result.data.get("parse")
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("text"), str):
        raise MediaWikiApiError(f"revision {page.revision_id} has no rendered HTML")
    raw_categories = parsed.get("categories", [])
    categories = (
        tuple(
            sorted(
                {
                    str(item["category"])
                    for item in raw_categories
                    if isinstance(item, Mapping) and item.get("category")
                },
                key=str.casefold,
            )
        )
        if isinstance(raw_categories, list)
        else ()
    )
    return RenderedMediaWikiPage(
        page=page,
        html=str(parsed["text"]).encode("utf-8"),
        categories=categories,
        api_response=result,
    )
