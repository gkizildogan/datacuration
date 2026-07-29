from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

TOKEN_RE = re.compile(r"\w+(?:['’.-]\w+)*", re.UNICODE)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).strip() + "\n"


def normalized_for_hash(value: str) -> str:
    value = normalize_text(value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = sha256_text(canonical)[:length]
    return f"{prefix}_{digest}"


def tokens(value: str) -> list[str]:
    return [match.group(0) for match in TOKEN_RE.finditer(value)]


def normalized_tokens(value: str) -> list[str]:
    return [token.casefold() for token in tokens(unicodedata.normalize("NFKC", value))]
