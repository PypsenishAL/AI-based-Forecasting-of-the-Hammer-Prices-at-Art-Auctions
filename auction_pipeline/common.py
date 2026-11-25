"""Shared validation, catalogue text and HTTP helpers."""

import io
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image


ART_AUCTION_TITLE = re.compile(
    r"\b(?:paintings?|old masters|contemporary art|modern art|(?:20th|21st) century)\b", re.I,
)
PAINTING = re.compile(
    r"(?P<paint>oil|acrylic|tempera|gouache|watercolou?r|distemper|encaustic|casein)"
    r"(?:\s+and\s+(?:oil|acrylic|tempera|gouache|watercolou?r|distemper|encaustic|casein"
    r"|marker|graphite|charcoal|pastel|ink))*\s+(?:on|over)\s+"
    r"(?P<support>(?:unprimed |primed |raw )?(?:canvas|linen)|(?:wood(?:en)? |artist['’]s )?panel"
    r"|masonite|wood|paper|paperboard|cardboard|board|copper)"
    r"(?:\s+(?:laid down|mounted) on (?:canvas|linen|board|panel))?"
    r"(?:,\s*(?:in (?:the )?artist['’]s frame|in \w+ parts))?\.?", re.I,
)
DIMENSIONS = re.compile(r"\d.*\s[x×]\s.*\d.*(?:\bin\.|\bcm\b|\bmm\b)", re.I)
CREATION = re.compile(r"(?:painted|executed|created)\s+(?:in|circa|c\.)\s+.*\b\d{4}\b", re.I)
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
MAX_BODY = 25 * 1024 * 1024
class PipelineError(Exception):
    """The source or input does not support a verified snapshot."""


class AuctionStatusError(PipelineError):
    """The source cannot confirm that the auction is still upcoming."""


class HistoryReadinessError(PipelineError):
    """The run could not produce a complete pre-auction history."""


class DeadlineError(PipelineError):
    """The selected session no longer has a pre-auction collection window."""


def utc_datetime(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"Unknown deadline or missing timezone: {value!r}") from exc


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now():
    return datetime.now(timezone.utc)


def check_deadline(start, now):
    if now() >= start:
        raise DeadlineError(f"Session deadline missed ({utc_text(start)})")


def checked_url(value, hosts):
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in hosts or url.port not in (None, 443)
            or url.username is not None or url.password is not None):
        raise PipelineError(f"Unexpected URL: {value}")
    return value


def fetch_resource(session, url, hosts):
    for _ in range(5):
        checked_url(url, hosts)
        with session.get(url, timeout=(8, 25), stream=True, allow_redirects=False) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise PipelineError("Response exceeds 25 MiB")
            return bytes(body)
    raise PipelineError("Too many redirects")


def plain_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    return section_text(soup)


def section_text(node):
    for tag in node.find_all(["script", "style"]):
        tag.decompose()
    for tag in node.find_all("br"):
        tag.replace_with("\n")
    for tag in node.find_all(["p", "li", "div", "tr", "td", "th"]):
        tag.insert_after("\n")
    return "\n".join(line for raw in node.get_text().splitlines() if (line := " ".join(raw.split())))


def painting_medium(text):
    line = painting_category(text)
    if not line:
        return ""
    match = PAINTING.fullmatch(line)
    return line if (
        match["paint"].lower() in {"oil", "acrylic", "tempera"}
        and not re.search(r"\b(?:mounted|laid down)\b", line, re.I)
        and re.search(r"\b(?:canvas|linen|panel|masonite)\b", match["support"], re.I)
    ) else ""


def painting_category(text):
    matches = [line for line in text.splitlines() if PAINTING.fullmatch(line)]
    return matches[0] if len(matches) == 1 else ""


def dimensions_text(description):
    return "\n".join(line for line in description.splitlines() if DIMENSIONS.search(line))


def matching_line(text, pattern):
    matches = [line for line in text.splitlines() if pattern.search(line)]
    return matches[0] if len(matches) == 1 else ""


def csv_text(text):
    # Escape backslashes first so a literal \n can be distinguished from a line break.
    return text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def parse_lot_number(value):
    match = re.fullmatch(r"([0-9]+)([A-Z]*)", value.strip(), re.I | re.ASCII)
    return (int(match[1]), match[2].upper()) if match and int(match[1]) else None


def in_lot_range(number, low, high):
    return (number is not None and low <= number <= high
            and (low[1] != high[1] or not low[1] or number[1] == low[1]))


def lot_label(number):
    return f"{number[0]}{number[1]}"


def estimate_data(lot, labels):
    raw = plain_text(lot.get("estimate_txt"))
    if lot.get("estimate_on_request") or lot.get("estimate_visible") is False or not raw:
        raise PipelineError("Estimate is not publicly displayed")
    currency_label = (labels.get("currency_txt") or "").strip().upper()
    currencies = set(re.findall(r"\b[A-Z]{3}\b", raw))
    if currency_label:
        currencies.add(currency_label)
    if len(currencies) != 1 or not re.fullmatch(r"[A-Z]{3}", next(iter(currencies))):
        raise PipelineError("Estimate currency is missing or ambiguous")
    try:
        low, high = (Decimal(str(lot[key])) for key in ("estimate_low", "estimate_high"))
    except (KeyError, InvalidOperation) as exc:
        raise PipelineError("Estimate bounds are missing or invalid") from exc
    if not low.is_finite() or not high.is_finite() or not 0 < low <= high:
        raise PipelineError("Estimate bounds are invalid")
    display = re.fullmatch(
        r"\s*(?:[A-Z]{3}\s*)?(?P<low>[0-9][0-9,. ]*?)\s*[-–—−]\s*"
        r"(?:[A-Z]{3}\s*)?(?P<high>[0-9][0-9,. ]*?)(?:\s*[A-Z]{3})?\s*", raw)
    try:
        shown = tuple(Decimal(display[key].replace(",", "").replace(" ", ""))
                      for key in ("low", "high")) if display else ()
    except InvalidOperation:
        shown = ()
    if shown != (low, high):
        raise PipelineError("Estimate bounds do not match the public display")
    return dict(estimate_low=format(low, "f"), estimate_high=format(high, "f"),
                estimate_currency=currencies.pop(), estimate_raw=raw)


def download_photo(fetch, urls, hosts):
    failures = []
    for url in urls:
        try:
            body = fetch(checked_url(url, hosts))
            with Image.open(io.BytesIO(body)) as image:
                extension = IMAGE_EXTENSIONS.get(image.format)
                if extension is None or min(image.size) < 100:
                    raise PipelineError("Image format unsupported or thumbnail too small")
                image.verify()
            with Image.open(io.BytesIO(body)) as image:
                image.load()
            return extension, body
        except (PipelineError, requests.RequestException, OSError, ValueError,
                Image.DecompressionBombError) as exc:
            failures.append(str(exc))
    raise PipelineError("No readable main image" + (f": {failures[-1]}" if failures else ""))
