"""Normalizacao de nomes, slugs, anos e notas.

Nao mapear status do Stash para status do Ja Zerei sem tabela validada.
"""

from __future__ import annotations

import re
import unicodedata

EDITION_TOKENS = (
    "remaster",
    "remastered",
    "remake",
    "hd",
    "definitive",
    "deluxe",
    "complete",
    "ultimate",
    "goty",
    "port",
    "collection",
)

WHITESPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_title(value: str) -> str:
    text = strip_accents(value).lower().replace("&", " and ")
    text = NON_WORD_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_slug(value: str) -> str:
    text = strip_accents(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def title_tokens(value: str) -> set[str]:
    return set(normalize_title(value).split())


def edition_tokens(value: str) -> set[str]:
    return title_tokens(value) & set(EDITION_TOKENS)


def extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = YEAR_RE.search(value)
    return int(match.group(0)) if match else None


def constrain_rating(value: float | int | None) -> float | None:
    """Escala observada no Ja Zerei: 0.5 a 10. Arredonda para 0.5."""
    if value is None:
        return None
    rating = max(0.0, min(10.0, float(value)))
    return round(rating * 2) / 2
