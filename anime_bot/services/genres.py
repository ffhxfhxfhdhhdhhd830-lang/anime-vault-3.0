"""Canonical, backward-compatible handling for catalogue genre values."""

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any


CANONICAL_GENRES = (
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Fantasy",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Supernatural",
    "Mystery",
    "Thriller",
    "Horror",
    "Sports",
    "Psychological",
    "Mecha",
    "Music",
    "School",
    "Historical",
    "Military",
    "Magic",
    "Demons",
    "Super Power",
    "Martial Arts",
    "Isekai",
    "Harem",
    "Reverse Harem",
    "Josei",
    "Seinen",
    "Shoujo",
    "Shounen",
    "Ecchi",
    "Parody",
)


def _key(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


_CANONICAL = {_key(name): name for name in CANONICAL_GENRES}
_ALIASES = {
    "science fiction": "Sci-Fi",
    "sci fi": "Sci-Fi",
    "scifi": "Sci-Fi",
    "slice of life": "Slice of Life",
    "sliceoflife": "Slice of Life",
    "super powers": "Super Power",
    "superpower": "Super Power",
    "superpowers": "Super Power",
    "martial art": "Martial Arts",
    "shojo": "Shoujo",
    "shoujo": "Shoujo",
    "shonen": "Shounen",
    "shounen": "Shounen",
    "seinen": "Seinen",
    "josei": "Josei",
    "isekai": "Isekai",
    "reverse harem": "Reverse Harem",
}


def _parts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        name = value.get("name") or value.get("title") or value.get("genre")
        return _parts(name)
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith(("[", "{")):
            try:
                parsed = json.loads(raw)
                if parsed != value:
                    return _parts(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        return [part.strip() for part in re.split(r"[,;|\n\r]+", raw) if part.strip()]
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            result.extend(_parts(item))
        return result
    return []


def normalize_genres(*values: Any) -> list[str]:
    """Convert strings, arrays, or provider genre objects to unique genre names."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _parts(value):
            key = _key(part)
            if not key:
                continue
            name = _CANONICAL.get(key) or _ALIASES.get(key)
            if not name:
                # Keep provider-specific values while ensuring stable whitespace/case.
                name = " ".join(word[:1].upper() + word[1:] for word in part.split())
            canonical_key = _key(name)
            if canonical_key not in seen:
                seen.add(canonical_key)
                result.append(name)
    return result


def genres_for(anime: dict[str, Any]) -> list[str]:
    """Read the canonical field and known legacy field names without mutating data."""
    return normalize_genres(
        anime.get("genres"),
        anime.get("genre"),
        anime.get("genre_names"),
        anime.get("genres_list"),
        anime.get("Genre"),
        anime.get("Genres"),
    )

