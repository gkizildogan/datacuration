from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

from aviation_data.acquisition import fetch_sources
from aviation_data.extraction import extract_sources
from aviation_data.models import SourceRegistry


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
    canonical = data_dir / documents[0].canonical_path
    assert "aircraft engine provides power" in canonical.read_text(encoding="utf-8").lower()

    manifest = [
        json.loads(line)
        for line in (data_dir / "manifests" / "source_records.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest[0]["source_version"] == "revision:456"
