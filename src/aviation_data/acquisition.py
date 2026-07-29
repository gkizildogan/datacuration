from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import random
import shutil
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from aiolimiter import AsyncLimiter

from aviation_data.adapters.mediawiki_api import (
    ApiResponse,
    MediaWikiApiError,
    discover_pages,
    render_page,
)
from aviation_data.ids import stable_id
from aviation_data.io import read_json, read_jsonl, write_json, write_jsonl
from aviation_data.models import RightsState, SourceDefinition, SourceRecord, SourceRegistry


class AcquisitionError(RuntimeError):
    pass


def detect_mime(path_or_url: str, payload: bytes, header: str | None = None) -> str:
    if header:
        value = header.split(";", maxsplit=1)[0].strip().casefold()
        if value and value != "application/octet-stream":
            return value
    signatures = (
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"\xef\xbb\xbf", "text/plain"),
    )
    for signature, mime in signatures:
        if payload.startswith(signature):
            return mime
    stripped = payload.lstrip()
    if stripped.startswith((b"<!DOCTYPE html", b"<!doctype html", b"<html")):
        return "text/html"
    if stripped.startswith((b"<?xml",)):
        return "application/xml"
    if stripped.startswith((b"{", b"[")):
        return "application/json"
    guessed, _ = mimetypes.guess_type(path_or_url)
    return guessed or "application/octet-stream"


def _content_path(data_dir: Path, sha256: str) -> Path:
    return data_dir / "raw" / "sha256" / sha256[:2] / sha256


def _storage_relative(data_dir: Path, path: Path) -> str:
    return path.relative_to(data_dir).as_posix()


def _record(
    source: SourceDefinition,
    snapshot: date,
    url: str,
    payload: bytes,
    mime: str,
    storage_path: str,
    *,
    status: int,
    headers: dict[str, str],
    redirects: list[str],
    final_url: str | None = None,
    source_version: str | None = None,
    title: str | None = None,
    fetch_recipe: dict[str, object] | None = None,
) -> SourceRecord:
    digest = hashlib.sha256(payload).hexdigest()
    return SourceRecord(
        source_record_id=stable_id(
            "src", source.source_id, url, snapshot.isoformat(), digest, length=32
        ),
        registry_source_id=source.source_id,
        publisher=source.publisher,
        source_family=source.source_family,
        canonical_url=final_url or url,
        request_url=url,
        redirect_chain=redirects,
        http_status=status,
        response_headers=headers,
        retrieved_at=datetime.now(UTC),
        snapshot_date=snapshot,
        source_version=(
            source_version
            or headers.get("x-source-version")
            or headers.get("etag")
            or headers.get("last-modified")
            or digest[:16]
        ),
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        sha256=digest,
        byte_size=len(payload),
        detected_mime=mime,
        storage_path=storage_path,
        native_format=source.native_format,
        title=title or source.title,
        languages=source.languages,
        topics=source.topics,
        authority_level=source.authority_level,
        rights=source.rights,
        fetch_recipe={
            "adapter": source.adapter,
            "url": url,
            "version_discovery": source.version_discovery,
            **(fetch_recipe or {}),
        },
    )


class Fetcher:
    def __init__(
        self,
        registry: SourceRegistry,
        registry_path: Path,
        data_dir: Path,
        *,
        allow_network: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.registry = registry
        self.registry_path = registry_path
        self.project_root = registry_path.resolve().parent.parent
        self.data_dir = data_dir
        self.allow_network = allow_network
        self.transport = transport
        self.limiters: dict[str, AsyncLimiter] = {}
        self.semaphores: dict[str, asyncio.Semaphore] = {}
        self.robots: dict[str, RobotFileParser] = {}
        self.errors: list[dict[str, str]] = []
        self.existing = read_jsonl(data_dir / "manifests" / "source_records.jsonl", SourceRecord)
        self.by_snapshot_key = {
            (record.registry_source_id, record.request_url, record.snapshot_date): record
            for record in self.existing
        }

    def _host_controls(
        self, source: SourceDefinition, url: str
    ) -> tuple[AsyncLimiter, asyncio.Semaphore]:
        host = urlparse(url).netloc.casefold()
        if host not in self.limiters:
            rate = source.requests_per_second or self.registry.project.default_requests_per_second
            concurrency = source.concurrency or self.registry.project.default_concurrency
            self.limiters[host] = AsyncLimiter(max_rate=rate, time_period=1.0)
            self.semaphores[host] = asyncio.Semaphore(concurrency)
        return self.limiters[host], self.semaphores[host]

    async def _robots_allowed(
        self, client: httpx.AsyncClient, source: SourceDefinition, url: str
    ) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots:
            robots_url = urljoin(origin, "/robots.txt")
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = await client.get(robots_url, follow_redirects=True, timeout=30)
                if response.status_code < 400:
                    parser.parse(response.text.splitlines())
                else:
                    parser.parse([])
            except httpx.HTTPError:
                # A missing/unreachable robots file does not create crawl permission,
                # but direct seed retrieval is allowed when no rule can be obtained.
                parser.parse([])
            self.robots[origin] = parser
        return self.robots[origin].can_fetch(self.registry.project.user_agent, url)

    def _save_payload(self, payload: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        destination = _content_path(self.data_dir, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise AcquisitionError(f"content-address collision at {destination}")
        else:
            temporary = destination.with_suffix(".part")
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
            destination.chmod(0o444)
        return digest, _storage_relative(self.data_dir, destination)

    async def _fetch_file(self, source: SourceDefinition, url: str, snapshot: date) -> SourceRecord:
        raw_path = url.removeprefix("file:")
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        if not path.is_relative_to(self.project_root):
            raise AcquisitionError(f"local source escapes project root: {path}")
        payload = path.read_bytes()
        max_bytes = source.max_bytes or self.registry.project.default_max_bytes
        if len(payload) > max_bytes:
            raise AcquisitionError(f"{path} exceeds byte limit {max_bytes}")
        _, storage_path = self._save_payload(payload)
        mime = detect_mime(str(path), payload)
        return _record(
            source,
            snapshot,
            url,
            payload,
            mime,
            storage_path,
            status=200,
            headers={
                "last-modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                "x-source-version": hashlib.sha256(payload).hexdigest(),
            },
            redirects=[],
        )

    async def _fetch_http(
        self,
        client: httpx.AsyncClient,
        source: SourceDefinition,
        url: str,
        snapshot: date,
    ) -> SourceRecord:
        if not self.allow_network:
            raise AcquisitionError("network acquisition disabled; pass --network explicitly")
        if not await self._robots_allowed(client, source, url):
            raise AcquisitionError("robots.txt disallows this project user-agent")
        limiter, semaphore = self._host_controls(source, url)
        previous = next(
            (
                record
                for record in reversed(self.existing)
                if record.registry_source_id == source.source_id and record.request_url == url
            ),
            None,
        )
        headers: dict[str, str] = {}
        if previous and previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous and previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
        max_bytes = source.max_bytes or self.registry.project.default_max_bytes
        partial_id = stable_id("partial", source.source_id, url, length=32)
        partial_path = self.data_dir / "raw" / ".partial" / f"{partial_id}.part"
        partial_metadata_path = partial_path.with_suffix(".json")
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                request_headers = {**headers, "Accept-Encoding": "identity"}
                resume_size = 0
                partial_metadata: dict[str, str] = {}
                if partial_path.exists() and partial_metadata_path.exists():
                    try:
                        partial_metadata = read_json(partial_metadata_path)
                    except (OSError, ValueError):
                        partial_metadata = {}
                    validator = partial_metadata.get("validator")
                    if partial_metadata.get("url") == url and validator:
                        resume_size = partial_path.stat().st_size
                        request_headers.pop("If-None-Match", None)
                        request_headers.pop("If-Modified-Since", None)
                        request_headers["Range"] = f"bytes={resume_size}-"
                        request_headers["If-Range"] = validator
                async with (
                    semaphore,
                    limiter,
                    client.stream(
                        "GET",
                        url,
                        headers=request_headers,
                        follow_redirects=True,
                        timeout=httpx.Timeout(60, connect=30),
                    ) as response,
                ):
                    if response.status_code == 304 and previous:
                        return previous.model_copy(
                            update={
                                "source_record_id": stable_id(
                                    "src",
                                    source.source_id,
                                    url,
                                    snapshot.isoformat(),
                                    previous.sha256,
                                    length=32,
                                ),
                                "snapshot_date": snapshot,
                                "retrieved_at": datetime.now(UTC),
                                "http_status": 304,
                            }
                        )
                    if response.status_code in {429, 503}:
                        retry_after = response.headers.get("retry-after")
                        delay = min(60.0, 2**attempt + random.random())
                        if retry_after:
                            try:
                                delay = min(300.0, float(retry_after))
                            except ValueError:
                                try:
                                    parsed = parsedate_to_datetime(retry_after)
                                    delay = max(
                                        0.0,
                                        min(
                                            300.0,
                                            (
                                                parsed - datetime.now(parsed.tzinfo or UTC)
                                            ).total_seconds(),
                                        ),
                                    )
                                except (TypeError, ValueError):
                                    pass
                        await asyncio.sleep(delay)
                        continue
                    if response.status_code == 416 and resume_size:
                        partial_path.write_bytes(b"")
                        write_json(
                            partial_metadata_path,
                            {"url": url, "validator": ""},
                        )
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length:
                        expected = int(content_length) + (
                            resume_size if response.status_code == 206 else 0
                        )
                        if expected > max_bytes:
                            raise AcquisitionError(
                                f"declared response size exceeds byte limit {max_bytes}"
                            )
                    append = response.status_code == 206 and resume_size > 0
                    total = resume_size if append else 0
                    validator = response.headers.get("etag") or response.headers.get(
                        "last-modified", ""
                    )
                    write_json(
                        partial_metadata_path,
                        {"url": url, "validator": validator},
                    )
                    with partial_path.open("ab" if append else "wb") as handle:
                        async for chunk in response.aiter_raw():
                            total += len(chunk)
                            if total > max_bytes:
                                raise AcquisitionError(f"response exceeds byte limit {max_bytes}")
                            handle.write(chunk)
                    payload = partial_path.read_bytes()
                    _, storage_path = self._save_payload(payload)
                    response_headers = {
                        key.casefold(): value
                        for key, value in response.headers.items()
                        if key.casefold()
                        in {
                            "content-type",
                            "content-length",
                            "etag",
                            "last-modified",
                            "date",
                        }
                    }
                    mime = detect_mime(
                        str(response.url),
                        payload,
                        response.headers.get("content-type"),
                    )
                    redirects = [str(item.url) for item in response.history]
                    record = _record(
                        source,
                        snapshot,
                        url,
                        payload,
                        mime,
                        storage_path,
                        status=response.status_code,
                        headers=response_headers,
                        redirects=redirects,
                        final_url=str(response.url),
                    )
                partial_path.unlink(missing_ok=True)
                partial_metadata_path.unlink(missing_ok=True)
                return record
            except (httpx.HTTPError, AcquisitionError) as exc:
                last_error = exc
                if attempt < 5:
                    await asyncio.sleep(min(30.0, 2**attempt + random.random()))
        raise AcquisitionError(str(last_error or "fetch failed"))

    async def _mediawiki_request(
        self,
        client: httpx.AsyncClient,
        source: SourceDefinition,
        endpoint: str,
        params: dict[str, str],
    ) -> ApiResponse:
        if not self.allow_network:
            raise AcquisitionError("network acquisition disabled; pass --network explicitly")
        request_url = str(httpx.URL(endpoint, params=params))
        # Wikimedia's shared robots.txt blocks /w/ to keep dynamic URLs out of
        # search-engine crawls. Applying RobotFileParser here would also block
        # every intentional Action API client, which is instead governed by the
        # Wikimedia API access and User-Agent policies.
        limiter, semaphore = self._host_controls(source, request_url)
        max_bytes = source.max_bytes or self.registry.project.default_max_bytes
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                async with limiter, semaphore:
                    response = await client.get(
                        endpoint,
                        params=params,
                        follow_redirects=True,
                        timeout=httpx.Timeout(60, connect=30),
                    )
                if response.status_code in {429, 503}:
                    retry_after = response.headers.get("retry-after")
                    delay = min(60.0, 2**attempt + random.random())
                    if retry_after:
                        with suppress(ValueError):
                            delay = min(300.0, float(retry_after))
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                if len(response.content) > max_bytes:
                    raise AcquisitionError(f"MediaWiki API response exceeds byte limit {max_bytes}")
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise MediaWikiApiError("MediaWiki API returned a non-object response")
                if isinstance(payload.get("error"), Mapping):
                    error = payload["error"]
                    code = str(error.get("code", "unknown"))
                    message = str(error.get("info", "MediaWiki API error"))
                    if code in {"maxlag", "ratelimited", "readonly"} and attempt < 5:
                        await asyncio.sleep(min(60.0, 2**attempt + random.random()))
                        continue
                    raise MediaWikiApiError(f"{code}: {message}")
                response_headers = {
                    key.casefold(): value
                    for key, value in response.headers.items()
                    if key.casefold() in {"content-length", "etag", "last-modified", "date"}
                }
                return ApiResponse(
                    data=payload,
                    request_url=str(response.request.url),
                    response_headers=response_headers,
                    redirect_chain=[str(item.url) for item in response.history],
                )
            except (httpx.HTTPError, ValueError, AcquisitionError, MediaWikiApiError) as exc:
                last_error = exc
                if attempt < 5:
                    await asyncio.sleep(min(30.0, 2**attempt + random.random()))
        raise AcquisitionError(str(last_error or "MediaWiki API request failed"))

    async def _fetch_mediawiki_source(
        self,
        client: httpx.AsyncClient,
        source: SourceDefinition,
        snapshot: date,
    ) -> list[SourceRecord]:
        config = source.mediawiki
        if config is None:
            self.errors.append(
                {
                    "source_id": source.source_id,
                    "url": source.seed_urls[0] if source.seed_urls else "",
                    "error": "AcquisitionError: mediawiki_api source has no mediawiki config",
                }
            )
            return []

        records: list[SourceRecord] = []
        total_bytes = 0
        for endpoint in source.seed_urls:

            async def request(url: str, params: dict[str, str]) -> ApiResponse:
                return await self._mediawiki_request(client, source, url, params)

            try:
                pages = await discover_pages(request, endpoint, config)
            except Exception as exc:
                self.errors.append(
                    {
                        "source_id": source.source_id,
                        "url": endpoint,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            for page in pages:
                source_version = f"revision:{page.revision_id}"
                previous = next(
                    (
                        record
                        for record in reversed(self.existing)
                        if record.registry_source_id == source.source_id
                        and record.snapshot_date == snapshot
                        and record.source_version == source_version
                    ),
                    None,
                )
                if previous is not None:
                    total_bytes += previous.byte_size
                    if total_bytes > config.max_total_bytes:
                        self.errors.append(
                            {
                                "source_id": source.source_id,
                                "url": page.permanent_url,
                                "error": (
                                    "AcquisitionError: existing snapshot exceeds "
                                    f"source byte limit {config.max_total_bytes}"
                                ),
                            }
                        )
                        return records
                    records.append(previous)
                    continue
                try:
                    rendered = await render_page(request, endpoint, config, page)
                    max_bytes = source.max_bytes or self.registry.project.default_max_bytes
                    if len(rendered.html) > max_bytes:
                        raise AcquisitionError(f"rendered page exceeds byte limit {max_bytes}")
                    if total_bytes + len(rendered.html) > config.max_total_bytes:
                        self.errors.append(
                            {
                                "source_id": source.source_id,
                                "url": page.permanent_url,
                                "error": (
                                    "AcquisitionError: source would exceed total byte limit "
                                    f"{config.max_total_bytes}"
                                ),
                            }
                        )
                        return records
                    _, storage_path = self._save_payload(rendered.html)
                    headers = {
                        **rendered.api_response.response_headers,
                        "content-type": "text/html; charset=utf-8",
                    }
                    records.append(
                        _record(
                            source,
                            snapshot,
                            rendered.api_response.request_url,
                            rendered.html,
                            "text/html",
                            storage_path,
                            status=200,
                            headers=headers,
                            redirects=rendered.api_response.redirect_chain,
                            final_url=page.permanent_url,
                            source_version=source_version,
                            title=page.title,
                            fetch_recipe={
                                "api_endpoint": endpoint,
                                "page_id": page.page_id,
                                "page_title": page.title,
                                "revision_id": page.revision_id,
                                "revision_timestamp": page.revision_timestamp,
                                "history_url": page.history_url,
                                "discovery_categories": config.category_titles,
                                "access_policy": (
                                    "https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy"
                                ),
                            },
                        )
                    )
                    total_bytes += len(rendered.html)
                except Exception as exc:
                    self.errors.append(
                        {
                            "source_id": source.source_id,
                            "url": page.permanent_url,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        return records

    async def run(self, snapshot: date) -> tuple[list[SourceRecord], list[dict[str, str]]]:
        records: list[SourceRecord] = list(self.existing)
        async with httpx.AsyncClient(
            headers={"User-Agent": self.registry.project.user_agent},
            transport=self.transport,
        ) as client:
            tasks = []
            for source in self.registry.sources:
                if not source.enabled or source.rights.state == RightsState.BLOCKED:
                    continue
                if source.adapter == "mediawiki_api":
                    if not self.allow_network:
                        continue
                    tasks.append(self._fetch_mediawiki_source(client, source, snapshot))
                    continue
                for url in source.seed_urls:
                    if urlparse(url).scheme in {"http", "https"} and not self.allow_network:
                        continue
                    key = (source.source_id, url, snapshot)
                    if key in self.by_snapshot_key:
                        continue
                    tasks.append(self._fetch_one(client, source, url, snapshot))
            results = await asyncio.gather(*tasks)
            for result in results:
                if isinstance(result, list):
                    records.extend(result)
                elif result is not None:
                    records.append(result)
        unique = {record.source_record_id: record for record in records}
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item.snapshot_date,
                item.registry_source_id,
                item.canonical_url,
                item.source_record_id,
            ),
        )
        write_jsonl(self.data_dir / "manifests" / "source_records.jsonl", ordered)
        write_json(self.data_dir / "manifests" / "fetch_errors.json", self.errors)
        return ordered, self.errors

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        source: SourceDefinition,
        url: str,
        snapshot: date,
    ) -> SourceRecord | None:
        try:
            if url.startswith("file:"):
                return await self._fetch_file(source, url, snapshot)
            if urlparse(url).scheme not in {"http", "https"}:
                raise AcquisitionError(f"unsupported URL scheme: {url}")
            return await self._fetch_http(client, source, url, snapshot)
        except Exception as exc:
            self.errors.append(
                {
                    "source_id": source.source_id,
                    "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return None


async def fetch_sources(
    registry: SourceRegistry,
    registry_path: Path,
    data_dir: Path,
    snapshot: date,
    *,
    allow_network: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[SourceRecord], list[dict[str, str]]]:
    fetcher = Fetcher(
        registry=registry,
        registry_path=registry_path,
        data_dir=data_dir,
        allow_network=allow_network,
        transport=transport,
    )
    return await fetcher.run(snapshot)


def copy_content_addressed(source: Path, data_dir: Path) -> tuple[str, Path]:
    """Import a local binary into immutable storage (used by adapters/tests)."""
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    destination = _content_path(data_dir, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(source, destination)
        destination.chmod(0o444)
    return digest, destination
