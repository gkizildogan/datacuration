from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

from aviation_data.acquisition import fetch_sources
from aviation_data.adapters.mediawiki_api import (
    ApiResponse,
    discover_pages,
    excluded_page_category_matches,
)
from aviation_data.extraction import extract_sources
from aviation_data.models import MediaWikiApiConfig, SourceRegistry


def _registry() -> SourceRegistry:
    return SourceRegistry.model_validate(
        {
            "project": {
                "name": "test",
                "contact": "maintainer@example.test",
                "user_agent": "test/0.1 (+mailto:maintainer@example.test)",
            },
            "sources": [
                {
                    "source_id": "wikipedia_test",
                    "enabled": True,
                    "adapter": "mediawiki_api",
                    "seed_urls": ["https://example.test/w/api.php"],
                    "publisher": "Wikipedia contributors",
                    "source_family": "wikipedia",
                    "authority_level": "community_reference",
                    "languages": ["en"],
                    "topics": ["engines"],
                    "expected_mime_types": ["text/html"],
                    "native_format": "html",
                    "update_cadence": "frozen_snapshot",
                    "version_discovery": "test revision query",
                    "selectors": {"content": ".mw-parser-output"},
                    "extraction": {"profile": "mediawiki_article_v1"},
                    "max_bytes": 100_000,
                    "mediawiki": {
                        "page_titles": ["Aircraft engine"],
                        "max_pages": 1,
                    },
                    "rights": {
                        "state": "open",
                        "license_id": "CC-BY-SA-4.0",
                        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                        "terms_url": "https://example.test/terms",
                        "reviewed_on": "2026-07-29",
                        "attribution": "Wikipedia contributors",
                        "release_source": True,
                        "release_derived_text": True,
                        "release_qa": True,
                    },
                }
            ],
        }
    )


def test_mediawiki_api_fetches_revision_pinned_html(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        params = request.url.params
        if params.get("action") == "query":
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "pageid": 123,
                                "title": "Aircraft engine",
                                "fullurl": "https://example.test/wiki/Aircraft_engine",
                                "revisions": [
                                    {
                                        "revid": 456,
                                        "timestamp": "2026-07-28T12:00:00Z",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if params.get("action") == "parse" and params.get("oldid") == "456":
            return httpx.Response(
                200,
                json={
                    "parse": {
                        "title": "Aircraft engine",
                        "pageid": 123,
                        "revid": 456,
                        "text": (
                            '<div class="mw-parser-output"><h2>Propulsion</h2>'
                            "<p>An aircraft engine provides power for flight.</p></div>"
                        ),
                    }
                },
            )
        return httpx.Response(404, json={"error": "unexpected request"})

    registry_path = tmp_path / "configs" / "sources.yaml"
    registry_path.parent.mkdir()
    registry_path.write_text("test fixture\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    registry = _registry()
    records, errors = asyncio.run(
        fetch_sources(
            registry,
            registry_path,
            data_dir,
            date(2026, 7, 29),
            allow_network=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert not errors
    assert len(records) == 1
    record = records[0]
    assert record.title == "Aircraft engine"
    assert record.source_version == "revision:456"
    assert record.canonical_url.endswith("title=Aircraft_engine&oldid=456")
    assert record.fetch_recipe["revision_id"] == 456
    assert record.fetch_recipe["history_url"].endswith("title=Aircraft_engine&action=history")
    assert any(request.url.params.get("oldid") == "456" for request in requests)
    assert not any(request.url.path == "/robots.txt" for request in requests)

    documents, extraction_errors = extract_sources(registry, data_dir)
    assert not extraction_errors
    assert len(documents) == 1
    assert documents[0].as_of == date(2026, 7, 28)
    canonical = data_dir / documents[0].canonical_path
    assert canonical.read_text(encoding="utf-8").startswith("# Aircraft engine")
    assert "aircraft engine provides power" in canonical.read_text(encoding="utf-8").lower()

    manifest = [
        json.loads(line)
        for line in (data_dir / "manifests" / "source_records.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest[0]["source_version"] == "revision:456"


def test_mediawiki_discovery_includes_one_bounded_subcategory_level() -> None:
    requests: list[dict[str, str]] = []

    async def request(endpoint: str, params: dict[str, str]) -> ApiResponse:
        del endpoint
        requests.append(params)
        if params.get("list") == "categorymembers":
            return ApiResponse(
                data={
                    "query": {
                        "categorymembers": [
                            {"pageid": 20, "title": "Category:Airports"},
                            {"pageid": 21, "title": "Category:Aircraft"},
                        ]
                    }
                },
                request_url="https://example.test/subcategories",
                response_headers={},
                redirect_chain=[],
            )
        category = params["gcmtitle"]
        page_id = 1 if category == "Category:Aviation" else 2
        return ApiResponse(
            data={
                "query": {
                    "pages": [
                        {
                            "pageid": page_id,
                            "title": f"Page {page_id}",
                            "fullurl": f"https://example.test/wiki/Page_{page_id}",
                            "revisions": [
                                {
                                    "revid": 100 + page_id,
                                    "timestamp": "2026-07-29T00:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            },
            request_url="https://example.test/pages",
            response_headers={},
            redirect_chain=[],
        )

    pages = asyncio.run(
        discover_pages(
            request,
            "https://example.test/w/api.php",
            MediaWikiApiConfig(
                category_titles=["Category:Aviation"],
                max_pages=3,
                subcategory_depth=1,
                max_subcategories=2,
            ),
        )
    )

    assert [page.page_id for page in pages] == [1, 2]
    queried_categories = [
        row["gcmtitle"] for row in requests if row.get("generator") == "categorymembers"
    ]
    assert queried_categories == [
        "Category:Aircraft",
        "Category:Airports",
        "Category:Aviation",
    ]


def test_mediawiki_discovery_accepts_category_with_no_article_pages() -> None:
    async def request(endpoint: str, params: dict[str, str]) -> ApiResponse:
        del endpoint, params
        return ApiResponse(
            data={"batchcomplete": True},
            request_url="https://example.test/empty-category",
            response_headers={},
            redirect_chain=[],
        )

    pages = asyncio.run(
        discover_pages(
            request,
            "https://example.test/w/api.php",
            MediaWikiApiConfig(
                category_titles=["Category:Empty"],
                max_pages=1,
            ),
        )
    )

    assert pages == []


def test_mediawiki_discovery_excludes_branches_and_balances_categories() -> None:
    requests: list[dict[str, str]] = []

    async def request(endpoint: str, params: dict[str, str]) -> ApiResponse:
        del endpoint
        requests.append(params)
        if params.get("list") == "categorymembers":
            return ApiResponse(
                data={
                    "query": {
                        "categorymembers": [
                            {"pageid": 100, "title": "Category:Air traffic controllers"},
                            {"pageid": 101, "title": "Category:Aviation films"},
                            {"pageid": 102, "title": "Category:Air traffic control systems"},
                        ]
                    }
                },
                request_url="https://example.test/subcategories",
                response_headers={},
                redirect_chain=[],
            )
        category = params["gcmtitle"]
        first_page_id = 1 if category == "Category:Air traffic control" else 3
        return ApiResponse(
            data={
                "query": {
                    "pages": [
                        {
                            "pageid": page_id,
                            "title": f"Technical page {page_id}",
                            "fullurl": f"https://example.test/wiki/Technical_page_{page_id}",
                            "revisions": [
                                {
                                    "revid": 200 + page_id,
                                    "timestamp": "2026-07-29T00:00:00Z",
                                }
                            ],
                        }
                        for page_id in (first_page_id, first_page_id + 1)
                    ]
                }
            },
            request_url="https://example.test/pages",
            response_headers={},
            redirect_chain=[],
        )

    pages = asyncio.run(
        discover_pages(
            request,
            "https://example.test/w/api.php",
            MediaWikiApiConfig(
                category_titles=["Category:Air traffic control"],
                excluded_category_titles=["Category:Air traffic controllers"],
                excluded_category_patterns=[r"(?i)(?:^| )films?(?:$| )"],
                max_pages=4,
                max_candidate_pages=4,
                max_pages_per_category=2,
                subcategory_depth=1,
                max_subcategories=3,
            ),
        )
    )

    assert [page.page_id for page in pages] == [1, 2, 3, 4]
    queried_categories = [
        row["gcmtitle"] for row in requests if row.get("generator") == "categorymembers"
    ]
    assert queried_categories == [
        "Category:Air traffic control",
        "Category:Air traffic control systems",
    ]
    assert {page.discovery_categories for page in pages} == {
        ("Category:Air traffic control",),
        ("Category:Air traffic control systems",),
    }


def test_mediawiki_fetch_quarantines_off_scope_page_category(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = request.url.params
        if params.get("action") == "query":
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "pageid": 1,
                                "title": "Aviation Drama",
                                "fullurl": "https://example.test/wiki/Aviation_Drama",
                                "revisions": [
                                    {
                                        "revid": 101,
                                        "timestamp": "2026-07-28T12:00:00Z",
                                    }
                                ],
                            },
                            {
                                "pageid": 2,
                                "title": "Runway safety",
                                "fullurl": "https://example.test/wiki/Runway_safety",
                                "revisions": [
                                    {
                                        "revid": 102,
                                        "timestamp": "2026-07-28T12:00:00Z",
                                    }
                                ],
                            },
                        ]
                    }
                },
            )
        if params.get("action") == "parse":
            revision_id = int(params["oldid"])
            category = "2026_films" if revision_id == 101 else "Runway_safety"
            return httpx.Response(
                200,
                json={
                    "parse": {
                        "text": (
                            '<div class="mw-parser-output">'
                            f"<p>Revision {revision_id} content.</p></div>"
                        ),
                        "categories": [{"category": category}],
                    }
                },
            )
        return httpx.Response(404, json={"error": "unexpected request"})

    payload = _registry().model_dump(mode="json")
    payload["sources"][0]["mediawiki"] = {
        "category_titles": ["Category:Aviation safety"],
        "excluded_page_category_patterns": [r"(?i)(?:^| )films?(?:$| )"],
        "max_pages": 1,
        "max_candidate_pages": 2,
        "max_pages_per_category": 2,
    }
    registry = SourceRegistry.model_validate(payload)
    registry_path = tmp_path / "configs" / "sources.yaml"
    registry_path.parent.mkdir()
    registry_path.write_text("test fixture\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    records, errors = asyncio.run(
        fetch_sources(
            registry,
            registry_path,
            data_dir,
            date(2026, 7, 29),
            allow_network=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert not errors
    assert [record.title for record in records] == ["Runway safety"]
    assert records[0].fetch_recipe["discovery_categories"] == ["Category:Aviation safety"]
    exclusions = json.loads((data_dir / "manifests" / "mediawiki_exclusions.json").read_text())
    assert [row["title"] for row in exclusions] == ["Aviation Drama"]
    assert exclusions[0]["matched_categories"] == ["2026_films"]
    assert any(request.url.params.get("prop") == "text|categories" for request in requests)


def test_mediawiki_page_exclusion_matches_compound_turkish_fiction_category() -> None:
    config = MediaWikiApiConfig(
        page_titles=["Aerodinamik"],
        excluded_page_category_patterns=[
            r"(?i)(?:film|kurgu|televizyon|dizi|roman|komplo|video oyun)"
        ],
    )

    assert excluded_page_category_matches(["Bilimkurgu_konuları"], config) == [
        "Bilimkurgu_konuları"
    ]
    assert excluded_page_category_matches(["Video_oyunları"], config) == ["Video_oyunları"]


def test_mediawiki_page_exclusion_matches_video_game_category() -> None:
    config = MediaWikiApiConfig(
        page_titles=["Aerodynamics"],
        excluded_page_category_patterns=[r"(?i)(?:video games?|game franchises?)"],
    )

    assert excluded_page_category_matches(["2007_video_games"], config) == ["2007_video_games"]


def test_mediawiki_page_exclusion_matches_biographical_book_category() -> None:
    config = MediaWikiApiConfig(
        page_titles=["Aerodinamik"],
        excluded_page_category_patterns=[r"(?i)(?:biyograf|otobiyograf|anı kitap|gezi kitap)"],
    )

    assert excluded_page_category_matches(["Biyografik_kitaplar"], config) == [
        "Biyografik_kitaplar"
    ]


def test_mediawiki_page_exclusion_matches_person_role_category() -> None:
    config = MediaWikiApiConfig(
        page_titles=["Aerodynamics"],
        excluded_page_category_patterns=[
            r"(?i)(?:^| )(?:controllers|aviators|pilots|pioneers|inventors|designers)(?:$| )"
        ],
    )

    assert excluded_page_category_matches(["English_aviators"], config) == ["English_aviators"]
