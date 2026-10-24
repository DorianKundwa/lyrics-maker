"""
Fetch lyrics for a song title + artist from several providers.

Providers are tried in the order of PROVIDERS; the first usable result wins.
Each provider is isolated: a network or parse error in one never stops the rest.

Synced lyrics (LRC) come only from LRCLIB. Genius and AZLyrics return plain
text, which the existing pipeline auto-times.

Remove a name from PROVIDERS to disable that source.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

PROVIDERS = ["lrclib", "genius", "azlyrics"]

TIMEOUT = 8
API_USER_AGENT = "LyricForge/1.0 (personal lyrics video tool)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LyricsResult = dict  # {"source": str, "synced": bool, "text": str}

# Genius adds structure that is not sung: section headers in any language
# ([Intro: Bulin 47], [Verso: ...], [Coro]), the page title line, and
# parenthetical annotations ((Chay, chay), (x4)).
_BRACKET_LINE_RE = re.compile(r"^\s*\[[^\]]*\]\s*$")
_GENIUS_TITLE_RE = re.compile(r"^\s*\[?\s*(?:Letra de|Lyrics for|Lyrics)\b.*$", re.IGNORECASE)
_INLINE_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_PAREN_RE = re.compile(r"\([^()]*\)")


def _clean_genius_text(text: str) -> str:
    """Keep only the lines a singer sings, matching what the aligner receives."""
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or _BRACKET_LINE_RE.match(s) or _GENIUS_TITLE_RE.match(s):
            continue
        s = _INLINE_BRACKET_RE.sub("", s)
        s = _PAREN_RE.sub("", s)
        s = " ".join(s.split())
        if s:
            out.append(s)
    return "\n".join(out)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _matches(wanted: str, found: Optional[str]) -> bool:
    """Loose match so 'Song (Live)' still matches 'Song' and vice versa."""
    a, b = _norm(wanted), _norm(found or "")
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _lrclib(title: str, artist: str) -> Optional[LyricsResult]:
    r = requests.get(
        "https://lrclib.net/api/search",
        params={"track_name": title, "artist_name": artist},
        headers={"User-Agent": API_USER_AGENT},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    plain: Optional[str] = None
    for item in r.json():
        if item.get("instrumental"):
            continue
        if not (_matches(title, item.get("trackName")) and _matches(artist, item.get("artistName"))):
            continue
        if item.get("syncedLyrics"):
            return {"source": "LRCLIB", "synced": True, "text": item["syncedLyrics"]}
        if plain is None and item.get("plainLyrics"):
            plain = item["plainLyrics"]
    if plain:
        return {"source": "LRCLIB", "synced": False, "text": plain}
    return None


def _genius(title: str, artist: str) -> Optional[LyricsResult]:
    token = os.environ.get("GENIUS_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    r = requests.get(
        "https://api.genius.com/search",
        params={"q": f"{artist} {title}"},
        headers={"Authorization": f"Bearer {token}", "User-Agent": API_USER_AGENT},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    for hit in r.json().get("response", {}).get("hits", []):
        if hit.get("type") != "song":
            continue
        song = hit.get("result", {})
        if not (
            _matches(title, song.get("title"))
            and _matches(artist, song.get("primary_artist", {}).get("name"))
        ):
            continue
        page = requests.get(song["url"], headers={"User-Agent": BROWSER_USER_AGENT}, timeout=TIMEOUT)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        parts = []
        for box in soup.select('[data-lyrics-container="true"]'):
            # Genius marks its contributor/translation header and description as excluded.
            for junk in box.select('[data-exclude-from-selection="true"]'):
                junk.decompose()
            for br in box.find_all("br"):
                br.replace_with("\n")
            parts.append(box.get_text())
        text = _clean_genius_text("\n".join(parts))
        if text:
            return {"source": "Genius", "synced": False, "text": text}
    return None


# AZLyrics serves a captcha page to modern Chrome User-Agents, but returns the
# lyrics to some old browser strings (confirmed by testing). We try these in
# order and stop at the first real lyrics page.
AZLYRICS_USER_AGENTS = [
    "Mozilla/5.0 (X11; U; Linux i686; en-US) AppleWebKit/532.0 (KHTML, like Gecko) Chrome/3.0.197.0 Safari/532.0",
    "Mozilla/5.0 (Linux; U; en-US) AppleWebKit/525.13 (KHTML, like Gecko) Chrome/0.2.149.27 Safari/525.13",
    "Mozilla/5.0 (Macintosh; U; Mac OS X 10_6_1; en-US) AppleWebKit/530.5 (KHTML, like Gecko) Chrome/ Safari/530.5",
]


def _azlyrics(title: str, artist: str) -> Optional[LyricsResult]:
    url = f"https://www.azlyrics.com/lyrics/{_norm(artist)}/{_norm(title)}.html"
    for user_agent in AZLYRICS_USER_AGENTS:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=TIMEOUT)
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        ringtone = soup.find("div", class_="ringtone")
        if ringtone is None:
            continue  # captcha or not-found page for this User-Agent; try the next one
        # The lyrics block is the first following <div> that has no class attribute.
        body = next((d for d in ringtone.find_next_siblings("div") if not d.get("class")), None)
        if body is None:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text().strip()
        if text:
            return {"source": "AZLyrics", "synced": False, "text": text}
    return None


_PROVIDER_FUNCS: dict[str, Callable[[str, str], Optional[LyricsResult]]] = {
    "lrclib": _lrclib,
    "genius": _genius,
    "azlyrics": _azlyrics,
}


def fetch_lyrics(title: str, artist: str) -> tuple[Optional[LyricsResult], dict[str, str]]:
    """Return (first_result_or_None, {provider: error_message} for failed providers)."""
    errors: dict[str, str] = {}
    for name in PROVIDERS:
        try:
            result = _PROVIDER_FUNCS[name](title, artist)
        except Exception as exc:  # isolate each provider
            errors[name] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        if result:
            return result, errors
    return None, errors
