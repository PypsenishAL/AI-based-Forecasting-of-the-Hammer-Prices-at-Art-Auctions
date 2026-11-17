"""Collect a pre-auction snapshot of one Christie's live session."""

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from PIL import Image, ImageOps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE = "https://www.christies.com"
CALENDAR = BASE + "/en/calendar"
PAGINATION = BASE + "/api/discoverywebsite/auctionpages/lotsearch"
ART_AUCTION_TITLE = re.compile(
    r"\b(?:paintings?|old masters|contemporary art|modern art|(?:20th|21st) century)\b", re.I,
)
LOT_FIELDS = (
    "lot_id", "lot_number", "sale_id", "lot_url", "artist", "title",
    "creation_date_raw", "medium_raw", "dimensions_raw", "details_raw",
    "provenance_raw", "exhibited_raw", "literature_raw", "lot_essay_raw",
    "condition_raw", "image_path", "collected_at_utc", "session_start_utc",
    "cutoff_source_url",
)
ESTIMATE_FIELDS = (
    "lot_id", "estimate_low", "estimate_high", "estimate_currency", "estimate_raw",
    "collected_at_utc",
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
IMAGE_HOSTS = {"www.christies.com", "www.christies-media.com",
               "onlineonly.christies.com", "cdn.christies.com"}
MAX_BODY = 25 * 1024 * 1024
MANIFEST = "collection.json"
HISTORY_FILE = "artist_history.json"
BACKFILL_SUFFIX = ".history-backfill.json"


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
        raise PipelineError(f"Unexpected Christie’s URL: {value}")
    return value


def fetch_resource(session, url):
    for _ in range(5):
        checked_url(url, IMAGE_HOSTS)
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


def page(fetch, url):
    checked_url(url, {"www.christies.com"})
    return fetch(url).decode("utf-8-sig")


def components(html, names):
    decoder = json.JSONDecoder()
    pattern = rf"\bwindow\.chrComponents\.(?:{names})\s*=\s*"
    for match in re.finditer(pattern, html):
        source = html[match.end():]
        # The auction component wraps JSON data in a JavaScript object literal.
        wrapper = re.match(r"{\s*data\s*:\s*", source)
        try:
            if wrapper:
                yield {"data": decoder.raw_decode(source, wrapper.end())[0]}
            else:
                value, _ = decoder.raw_decode(source)
                if isinstance(value, dict):
                    yield value
        except json.JSONDecodeError:
            continue


def component(html, name):
    for value in components(html, name):
        if isinstance(value.get("data"), dict):
            return value
    raise PipelineError(f"Missing Christie’s component: {name}")


def confirm_upcoming(status, source, allow_live_sale=False):
    in_progress = status.get("is_in_progress")
    if (status.get("is_auction_over") is not False
            or not (in_progress is False or (in_progress is True and allow_live_sale))):
        raise AuctionStatusError(f"Upcoming auction status is unconfirmed or conflicts with time: {source}")


def live_auction(html, source):
    auction = component(html, "auction")["data"]
    status = auction.get("auction_status") or {}
    if auction.get("event_type") != "Sale":
        raise PipelineError("This loader requires a live auction")
    confirm_upcoming(status, source, allow_live_sale=True)
    if not all(re.fullmatch(r"[A-Za-z0-9]+", auction.get(key) or "")
               for key in ("sale_id", "sale_number", "sale_room_code")):
        raise PipelineError("Missing auction identifiers")
    return auction, status


def numeric_sessions(status):
    for session in status.get("sessions") or []:
        bounds = re.fullmatch(r"Lots?\s+([0-9]+[A-Z]*)(?:\s*[-–—]\s*([0-9]+[A-Z]*))?",
                              session.get("lot_txt") or "", re.I)
        if bounds:
            low, high = parse_lot_number(bounds[1]), parse_lot_number(bounds[2] or bounds[1])
            if low is not None and high is not None and low <= high:
                yield low, high, session


def confirm_schedule(html, source, lower, upper, expected, now=utc_now):
    auction, status = live_auction(html, source)
    if status.get("sessions") == []:
        official = utc_datetime(status.get("start_date"))
    else:
        matching = [session for low, high, session in numeric_sessions(status)
                    if in_lot_range(lower, low, high) and in_lot_range(upper, low, high)]
        if len(matching) != 1:
            raise PipelineError("Official schedule does not identify a single session for the lot range")
        official = utc_datetime(matching[0].get("date_time_universal"))
    if expected is not None and official != expected:
        raise PipelineError(f"Session start conflicts with official schedule ({utc_text(official)})")
    earlier_starts = set()
    observed = now()
    if status.get("is_in_progress") is True and observed < official:
        for low, high, session in numeric_sessions(status):
            if high < lower or low > upper or (
                    low[1] == high[1] and lower[1] == upper[1] and low[1] != lower[1]):
                when = utc_datetime(session.get("date_time_universal"))
                if when < official and when <= observed:
                    earlier_starts.add(when)
    confirm_upcoming(status, source, allow_live_sale=bool(earlier_starts))
    auction["earlier_session_starts"] = earlier_starts
    return auction, official


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


def catalogue_sections(soup):
    sections = {}
    for item in soup.select("chr-accordion-item"):
        heading = item.select_one('[slot="header"]')
        content = item.select_one('[slot="content"]')
        if heading and content:
            sections[heading.get_text(" ", strip=True).casefold()] = section_text(content)
    return sections


def lot_identity(url):
    checked_url(url, {"www.christies.com"})
    match = re.fullmatch(r"/en/lot/lot-(\d+)/?", urlsplit(url).path)
    if not match:
        raise PipelineError("Expected a Christie’s lot URL")
    return match[1]


def lot_number(row):
    return parse_lot_number(str(row.get("lot_id_txt", "")))


def parse_lot_number(value):
    match = re.fullmatch(r"([0-9]+)([A-Z]*)", value.strip(), re.I | re.ASCII)
    return (int(match[1]), match[2].upper()) if match and int(match[1]) else None


def in_lot_range(number, low, high):
    return (number is not None and low <= number <= high
            and (low[1] != high[1] or not low[1] or number[1] == low[1]))


def lot_label(number):
    return f"{number[0]}{number[1]}"


def reject_realised(row, earlier_starts=()):
    amount = row.get("price_realised")
    published = row.get("price_realised_txt") or ""
    if amount not in (None, ""):
        try:
            amount = Decimal(str(amount).replace(",", ""))
            if not amount.is_finite() or amount != 0:
                raise PipelineError("Published or unrecognised realised price")
        except InvalidOperation as exc:
            raise PipelineError("Unrecognised realised price") from exc
    for match in re.findall(r"\d[\d,.]*", published):
        if any(digit in "123456789" for digit in match):
            raise PipelineError("Published realised price")
    if any(row.get(key) for key in ("lot_withdrawn", "is_unsold", "is_auction_over")):
        raise PipelineError("Lot is withdrawn, unsold, closed or in progress")
    if row.get("is_in_progress"):
        if not earlier_starts or utc_datetime(row.get("start_date")) not in earlier_starts:
            raise PipelineError("Lot is in progress")


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


def card_data(html, identity, auction):
    for header in components(html, r"lotHeader_\d+"):
        data = header.get("data") or {}
        for lot in data.get("lots") or []:
            if str(lot.get("object_id")) != identity:
                continue
            sale = data.get("sale") or {}
            if any(str(sale.get(key)) != str(auction[key]) for key in
                   ("sale_id", "sale_number", "sale_room_code")):
                raise PipelineError("Lot belongs to another or unknown auction")
            confirm_upcoming(sale, f"lot {identity}", bool(auction.get("earlier_session_starts")))
            return lot, header.get("labels") or {}
    raise PipelineError("No header record for the lot object ID")


def photo_candidates(soup, preview, lot_url):
    image = soup.select_one('[region="lot-header-image"] img[data-srcset]')
    candidates = []
    if image:
        for src in image["data-srcset"].split(","):
            parts = src.split()
            if len(parts) == 2 and re.fullmatch(r"\d+w", parts[-1]):
                candidates.append((int(parts[-1][:-1]), parts[0]))
    urls = [urljoin(lot_url, src) for _, src in sorted(candidates, reverse=True)]
    fallback = (preview.get("image") or {}).get("image_src")
    if fallback:
        urls.append(urljoin(lot_url, fallback))
    return dict.fromkeys(urls)


def download_photo(fetch, urls):
    failures = []
    for url in urls:
        try:
            body = fetch(checked_url(url, IMAGE_HOSTS))
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


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        if reader.fieldnames != list(fields):
            raise PipelineError(f"Unexpected CSV columns: {path.name}")
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise PipelineError(f"Malformed CSV record: {path.name}")
        return rows


def collection_name(auction, args, collected):
    title = plain_text(auction.get("title") or "Christie's live auction")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    title = " ".join(title.split())[:65].rstrip(" .") or "Christie's live auction"
    return (f"{title} - {auction['sale_number']} - lots {lot_label(args.lot_from)}-{lot_label(args.lot_to)}"
            f" - {collected:%Y-%m-%d %H-%M-%S} UTC")


def is_link(path):
    file_stat = path.lstat()
    return (stat.S_ISLNK(file_stat.st_mode)
            or bool(getattr(file_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def hash_files(folder):
    """Fingerprint only the expected, flat snapshot layout."""
    entries = {entry.name: entry for entry in folder.iterdir()}
    allowed = {"lots.csv", "estimates.csv", "errors.txt", "images", MANIFEST, HISTORY_FILE}
    if ("images" not in entries or set(entries) - allowed
            or any(is_link(entry) for entry in entries.values())):
        raise PipelineError("Snapshot contains additional files or links")
    images = entries.pop("images")
    entries.pop(MANIFEST, None)
    files = entries
    for photo in images.iterdir():
        if (is_link(photo) or not photo.is_file() or not photo.stem.isascii()
                or not photo.stem.isdecimal() or photo.suffix not in IMAGE_EXTENSIONS.values()):
            raise PipelineError("Snapshot contains an unexpected image entry")
        files[f"images/{photo.name}"] = photo
    hashes = {}
    for name, path in files.items():
        with path.open("rb") as source:
            hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
    return hashes


def write_json(path, payload):
    temp = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_manifest(folder, start, ready_at=None):
    payload = {"version": 1, "session_start_utc": utc_text(start),
               "analysis_ready_at_utc": utc_text(ready_at) if ready_at else None,
               "files": hash_files(folder)}
    write_json(folder / MANIFEST, payload)


def checked_history(history, lots, start, ready_at):
    ids = [row["lot_id"] for row in lots]
    if (history.get("schema_version") != 2 or history.get("coverage_status") != "scanned"
            or history.get("source_errors") != []
            or history.get("session_start_utc") != utc_text(start)
            or [row.get("lot_id") for row in history.get("lots", [])] != ids
            or any("hammer_comparables" not in row for row in history.get("lots", []))):
        raise HistoryReadinessError("Historical coverage or lot alignment is incomplete")
    observed = [utc_datetime(row["collected_at_utc"]) for row in lots]
    generated = utc_datetime(history.get("generated_at_utc"))
    if not (utc_datetime(history.get("cutoff_utc")) == min(observed)
            and max(observed) <= generated <= ready_at < start
            and all(utc_datetime(row["session_start_utc"]) == start for row in lots)):
        raise HistoryReadinessError("History timestamps violate the shared session deadline")


def cleanup_expired(root, current, now):
    for folder in root.iterdir():
        if folder == current or folder.name == "used" or not folder.is_dir():
            continue
        try:
            manifest = folder / MANIFEST
            if is_link(folder) or folder.resolve().parent != root.resolve():
                continue
            if not manifest.is_file() or is_link(manifest):
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or utc_datetime(data.get("session_start_utc")) > now()
                    or data.get("files") != hash_files(folder)):
                continue
            if (folder / "lots.csv").exists():
                rows = read_csv(folder / "lots.csv", LOT_FIELDS)
                if any(row["session_start_utc"] != data["session_start_utc"] for row in rows):
                    continue
            shutil.rmtree(folder)
            print(f"Removed expired snapshot: {folder}", file=sys.stderr)
        except (PipelineError, OSError, KeyError, ValueError, csv.Error) as exc:
            print(f"Cleanup skipped {folder}: {exc}", file=sys.stderr)


def free_name(root, name):
    destination = root / name
    if destination.exists():
        destination = root / f"{name} ({uuid4().hex[:8]})"
    return destination


PRICE_WORD = re.compile(r"\b(?:estim(?:ate|ated|ation)|bid(?:ding)?|price|reserve|hammer|realised|realized)\b", re.I)
SENSITIVE_VALUE = re.compile(
    r"\b(?:usd|gbp|eur|chf|hkd|cny|jpy|aed|aud|cad|dollars?|pounds?|euros?)\b|[$€£¥]|https?://|www\."
    r"|\b\d{1,3}(?:[, ]\d{3})+\b|\b\d{5,}\b", re.I,
)
STANDALONE_AMOUNT = re.compile(r"\s*[\d,. ]+(?:\s*[-–—−]\s*[\d,. ]+)?\s*")
BLIND_FIELDS = ("artist", "title", "creation_date_raw", "medium_raw", "dimensions_raw",
                "details_raw", "provenance_raw", "exhibited_raw", "literature_raw",
                "lot_essay_raw", "condition_raw")


def blind_text(value, identity_field=False):
    retained = []
    after_price_label = False
    for line in value.replace(r"\n", "\n").splitlines():
        if not line.strip():
            continue
        price_label = not identity_field and bool(PRICE_WORD.search(line))
        if not (price_label or SENSITIVE_VALUE.search(line)
                or (after_price_label and STANDALONE_AMOUNT.fullmatch(line))):
            retained.append(line)
        after_price_label = price_label
    return "\n".join(retained)


def write_blind_packet(folder, snapshot, lots, history):
    images = folder / "images"
    images.mkdir(parents=True)
    by_id = {row["lot_id"]: row for row in history["lots"]}
    packet = []
    for index, row in enumerate(lots, 1):
        sample_id = f"work-{index:02d}"
        image_path = f"images/{sample_id}.png"
        with Image.open(snapshot / row["image_path"]) as source:
            clean = ImageOps.exif_transpose(source).convert("RGB")
            clean.info.clear()
            clean.save(folder / image_path, format="PNG")
        comparables = by_id[row["lot_id"]]["hammer_comparables"]
        fields = ("title", "medium", "dimensions_raw", "sale_date", "currency", "hammer_price")
        packet.append({
            "sample_id": sample_id, "image_path": image_path,
            **{key: blind_text(row[key], identity_field=key in {"artist", "title"}) for key in BLIND_FIELDS},
            "hammer_comparables": {
                "price_basis": "hammer_price", "status": comparables["status"],
                "groups": comparables["groups"],
                "sales": [{key: blind_text(sale[key], identity_field=key == "title")
                           if key in {"title", "medium", "dimensions_raw"}
                           else sale[key] for key in fields} for sale in comparables["sales"]],
            },
        })
    write_json(folder / "lots.json", {"prediction_deadline_utc": history["session_start_utc"], "lots": packet})


def archive_used_lots(snapshot, lot_ids):
    if not lot_ids:
        raise PipelineError("Supply at least one --lot-id")
    root = snapshot.absolute().parent
    with FileLock(root / ".collections.lock", timeout=30):
        if not snapshot.is_dir() or is_link(snapshot) or snapshot.name.endswith(".incomplete"):
            raise PipelineError("Select a completed snapshot")
        hashes = hash_files(snapshot)
        manifest_path = snapshot / MANIFEST
        if not manifest_path.is_file() or HISTORY_FILE not in hashes:
            raise HistoryReadinessError("Snapshot is not ready for analysis")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("analysis_ready_at_utc") or manifest.get("files") != hashes:
            raise HistoryReadinessError("Snapshot is not ready for analysis or has changed")
        ready_at = utc_datetime(manifest["analysis_ready_at_utc"])
        start = utc_datetime(manifest["session_start_utc"])
        all_lots = read_csv(snapshot / "lots.csv", LOT_FIELDS)
        history = json.loads((snapshot / HISTORY_FILE).read_text(encoding="utf-8"))
        checked_history(history, all_lots, start, ready_at)
        lots = [row for row in all_lots if row["lot_id"] in lot_ids]
        estimates = [row for row in read_csv(snapshot / "estimates.csv", ESTIMATE_FIELDS)
                     if row["lot_id"] in lot_ids]
        for rows in (lots, estimates):
            if len(rows) != len(lot_ids) or {row["lot_id"] for row in rows} != lot_ids:
                raise PipelineError("Each requested lot must occur once in both CSV files")
        timestamps = {row["lot_id"]: row["collected_at_utc"] for row in estimates}
        if any(row["collected_at_utc"] != timestamps[row["lot_id"]] for row in lots):
            raise PipelineError("Catalog and estimate timestamps disagree")
        for row in lots:
            photo = row["image_path"]
            if photo not in hashes or not photo.startswith(f"images/{row['lot_id']}."):
                raise PipelineError(f"Missing or inconsistent image for lot {row['lot_id']}")
        if snapshot.name == "used" or root.name == "used":
            raise PipelineError("Select a snapshot from the collection folder")
        archive = root / "used"
        archive.mkdir(exist_ok=True)
        if is_link(archive) or archive.resolve().parent != root.resolve():
            raise PipelineError("The used archive must be a local directory inside the collection folder")
        name = f"{snapshot.name} - used"
        staging = archive / f".archive-{uuid4().hex[:8]}.incomplete"
        audit = staging / "audit"
        (audit / "images").mkdir(parents=True)
        try:
            write_csv(audit / "lots.csv", LOT_FIELDS, lots)
            write_csv(audit / "estimates.csv", ESTIMATE_FIELDS, estimates)
            for row in lots:
                shutil.copyfile(snapshot / row["image_path"], audit / row["image_path"])
            if "errors.txt" in hashes:
                shutil.copyfile(snapshot / "errors.txt", audit / "errors.txt")
            history["lots"] = [row for row in history["lots"] if row["lot_id"] in lot_ids]
            write_json(audit / HISTORY_FILE, history)
            write_json(audit / "source_collection.json", manifest)
            write_json(audit / "sample_map.json", {f"work-{i:02d}": row["lot_id"]
                                                   for i, row in enumerate(lots, 1)})
            write_json(audit / "archive_time.json", {"archived_at_utc": utc_text(utc_now())})
            write_blind_packet(staging / "blind", snapshot, lots, history)
            arbiter = staging / "arbiter"
            arbiter.mkdir()
            by_id = {row["lot_id"]: row for row in estimates}
            write_json(arbiter / "estimates.json", {"lots": [
                {"sample_id": f"work-{i:02d}", **{key: by_id[row["lot_id"]][key]
                 for key in ("estimate_low", "estimate_high", "estimate_currency")}}
                for i, row in enumerate(lots, 1)
            ]})
            destination = free_name(archive, name)
            staging.rename(destination)
        finally:
            if staging.is_dir() and not is_link(staging) and staging.resolve().parent == archive.resolve():
                shutil.rmtree(staging)
        return destination


def log_entry(log, url, stage, exc):
    log.append((url, stage, " ".join(str(exc).split())))


def write_log(path, log):
    path.write_text("url\tstage\treason\n" + "".join(
        f"{url}\t{stage}\t{reason}\n" for url, stage, reason in log), encoding="utf-8")


def live_catalogue_rows(data, url, fetch):
    initial = data.get("lots")
    if not isinstance(initial, list) or any(not isinstance(row, dict) for row in initial):
        raise PipelineError("Invalid catalogue lot list")
    rows = list(initial)
    total = data.get("total_hits_filtered")
    if type(total) is not int or total < len(rows):
        raise PipelineError("Invalid catalogue lot count")
    errors = []
    if total > len(rows):
        endpoint = data.get("lot_search_api_endpoint") or {}
        params = endpoint.get("parameters") or {}
        # The public page supplies the sale identifiers and the page size.
        if endpoint.get("path", "").lstrip("/") != urlsplit(PAGINATION).path.lstrip("/") or not all(
                str(params.get(key, "")) for key in ("saleid", "salenumber", "saleroomcode")):
            errors.append({"url": url, "error": "Missing historical catalogue pagination"})
        else:
            try:
                page_size = int(params.get("pagesize") or 0)
            except (TypeError, ValueError) as exc:
                raise PipelineError("Invalid historical catalogue page size") from exc
            if not 0 < page_size <= 200 or page_size != len(rows) or total > 10000:
                raise PipelineError("Unexpected historical catalogue page size")
            for page_number in range(2, (total + page_size - 1) // page_size + 1):
                query = urlencode({
                    "language": "en", "pagesize": page_size, "saleid": params["saleid"],
                    "salenumber": params["salenumber"], "saleroomcode": params["saleroomcode"],
                    "page": page_number, "sortby": "lotnumber", "saletype": "Sale",
                })
                page_url = f"{PAGINATION}?{query}"
                try:
                    following = json.loads(page(fetch, page_url))
                    if not isinstance(following, dict):
                        raise PipelineError("Invalid following catalogue page")
                    additional = following.get("lots")
                    if (following.get("total_hits_filtered") != total
                            or not isinstance(additional, list) or not additional
                            or any(not isinstance(row, dict) for row in additional)):
                        raise PipelineError("Invalid following historical catalogue page")
                    rows.extend(additional)
                except (PipelineError, requests.RequestException, UnicodeError, ValueError) as exc:
                    errors.append({"url": page_url, "error": str(exc)})
    identities = {str(row.get("object_id") or "") for row in rows}
    if "" in identities or len(rows) != total or len(identities) != total:
        errors.append({"url": url, "error": "Catalogue unique lot count differs from reported total"})
    return rows, errors


def current_catalogue_rows(html, url, fetch):
    rows, errors = live_catalogue_rows(component(html, "lots")["data"], url, fetch)
    if errors:
        raise PipelineError(f"Unable to read full catalog: {errors[0]['error']}")
    return rows


def candidate_rows(args, fetch, auction, log, now=utc_now):
    if args.lot_urls_file is not None:
        urls = args.lot_urls_file.read_text(encoding="utf-8-sig").splitlines()
        rows = []
        for url in dict.fromkeys(line.strip() for line in urls if line.strip()):
            try:
                identity = lot_identity(url)
                html = page(fetch, url)
                row, labels = card_data(html, identity, auction)
                rows.append((row, url, html, labels))
            except AuctionStatusError:
                raise
            except (PipelineError, requests.RequestException, UnicodeError) as exc:
                log_entry(log, url, "manual-card", exc)
        return rows
    html = page(fetch, args.catalog_url)
    catalog_sale, _ = confirm_schedule(html, args.catalog_url, args.lot_from,
                                       args.lot_to, args.session_start, now)
    if any(catalog_sale[key] != auction[key] for key in ("sale_id", "sale_number", "sale_room_code")):
        raise PipelineError("Catalog and schedule belong to different auctions")
    rows = current_catalogue_rows(html, args.catalog_url, fetch)
    if not isinstance(rows, list) or not rows:
        raise PipelineError("Empty catalog. Supply manually collected URLs with --lot-urls-file.")
    return [(row, urljoin(args.catalog_url, row.get("url") or ""), None, None) for row in rows]


def calendar_auctions(html):
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select('h3 a[data-qa="auction-card-primary-link"][href]'):
        if not ART_AUCTION_TITLE.search(link.get_text(" ", strip=True)):
            continue
        url = urlsplit(urljoin(CALENDAR, link["href"]))
        if url.scheme == "https" and url.netloc == "www.christies.com" and re.fullmatch(
                r"/en/auction/auction-\d+(?:-[a-z]+)?/?", url.path, re.I):
            yield BASE + url.path.rstrip("/")


def discover_and_collect(args, fetch, now, history_builder=None):
    today = now()
    seen = set()
    last_error = None
    fetch_errors = (PipelineError, requests.RequestException, UnicodeError)
    for offset in range(3):
        year, month = divmod(today.year * 12 + today.month - 1 + offset, 12)
        calendar_url = CALENDAR if offset == 0 else f"{CALENDAR}/{year}/{month + 1:02d}"
        try:
            auctions = list(dict.fromkeys(calendar_auctions(page(fetch, calendar_url))))
        except fetch_errors as exc:
            last_error = exc
            continue
        upcoming = []
        for base in auctions:
            if base in seen:
                continue
            seen.add(base)
            overview = base + "/overview"
            catalog = base + "/browse-lots"
            try:
                _, status = live_auction(page(fetch, overview), overview)
                if status.get("sessions") == []:
                    start = utc_datetime(status.get("start_date"))
                    if start > now():
                        upcoming.append((start, catalog, overview, None, None))
                for low, high, session in numeric_sessions(status):
                    start = utc_datetime(session.get("date_time_universal"))
                    if start > now() and high[0] - low[0] >= 9:
                        upcoming.append((start, catalog, overview, low, high))
            except fetch_errors as exc:
                last_error = exc
        catalogs = {}
        for start, catalog, overview, low, high in sorted(upcoming, key=lambda item: item[0]):
            try:
                check_deadline(start, now)
                if catalog not in catalogs:
                    preview = page(fetch, catalog)
                    rows = current_catalogue_rows(preview, catalog, fetch)
                    catalogs[catalog] = [lot_number(row) for row in rows
                                         if painting_medium(plain_text(row.get("description_txt")))]
                numbers = {number for number in catalogs[catalog] if number is not None}
                if not numbers:
                    continue
                if low is None:
                    low, high = min(numbers), max(numbers)
                if sum(in_lot_range(number, low, high) for number in numbers) < 10:
                    continue
                selection = argparse.Namespace(**(vars(args) | {
                    "catalog_url": catalog, "overview_url": overview,
                    "lot_from": low, "lot_to": high, "session_start": start,
                }))
                return collect(selection, fetch, now, history_builder)
            except HistoryReadinessError:
                raise
            except fetch_errors as exc:
                last_error = exc
                print(f"Skipping session {overview} ({utc_text(start)}): {exc}", file=sys.stderr)
    message = "No upcoming live painting session with at least 10 qualifying lots on the published catalog pages"
    if last_error is not None:
        message += f". Last source error: {last_error}"
    raise PipelineError(message)


def collect(args, fetch, now=utc_now, history_builder=None):
    if args.catalog_url is None:
        return discover_and_collect(args, fetch, now, history_builder)
    if args.session_start is not None:
        check_deadline(args.session_start, now)
    auction, start = confirm_schedule(page(fetch, args.overview_url), args.overview_url,
                                      args.lot_from, args.lot_to, args.session_start, now)
    check_deadline(start, now)
    args = argparse.Namespace(**(vars(args) | {"session_start": start}))
    log = []
    name = collection_name(auction, args, now())
    staging = args.output_dir / f"{name} ({uuid4().hex[:8]}).incomplete"
    (staging / "images").mkdir(parents=True)
    lots, estimates, seen = [], [], set()
    try:
        candidates = candidate_rows(args, fetch, auction, log, now)
        candidates.sort(key=lambda candidate: lot_number(candidate[0]) or (0, ""))
        for row, url, html, labels in candidates:
            check_deadline(start, now)
            number = lot_number(row)
            stage = "catalog"
            try:
                if not in_lot_range(number, args.lot_from, args.lot_to):
                    raise PipelineError("Lot number outside the selected session range")
                identity = lot_identity(url)
                if identity != str(row.get("object_id")):
                    raise PipelineError("Catalog object ID does not match lot URL")
                if identity in seen:
                    raise PipelineError("Duplicate lot object ID")
                reject_realised(row, auction["earlier_session_starts"])
                if html is None and not painting_medium(plain_text(row.get("description_txt"))):
                    raise PipelineError("Technique excluded by the catalog painting filter")
                stage = "card"
                if html is None:
                    html = page(fetch, url)
                    lot, labels = card_data(html, identity, auction)
                else:
                    lot = row
                if lot_number(lot) != number:
                    raise PipelineError("Lot number in the card conflicts with the catalog")
                reject_realised(lot)
                soup = BeautifulSoup(html, "html.parser")
                sections = catalogue_sections(soup)
                details = sections.get("details", "")
                medium = painting_medium(details)
                artist = plain_text(lot.get("title_primary_txt"))
                title = plain_text(lot.get("title_secondary_txt"))
                if not medium or not artist or not title:
                    raise PipelineError("Details do not confirm a painting, artist or title")
                stage = "estimate"
                estimate = estimate_data(lot, labels)
                stage = "image"
                extension, body = download_photo(fetch, photo_candidates(soup, row, url))
            except AuctionStatusError:
                raise
            except (PipelineError, requests.RequestException, UnicodeError) as exc:
                log_entry(log, url, stage, exc)
                continue
            collected = now()
            check_deadline(start, lambda: collected)
            image_path = f"images/{identity}{extension}"
            (staging / image_path).write_bytes(body)
            lot_row = dict(
                lot_id=identity, lot_number=lot_label(number), sale_id=auction["sale_id"],
                lot_url=BASE + f"/en/lot/lot-{identity}", artist=artist, title=title,
                creation_date_raw=matching_line(details, CREATION), medium_raw=medium,
                dimensions_raw=dimensions_text(details),
                details_raw=details,
                provenance_raw=sections.get("provenance", ""),
                exhibited_raw=sections.get("exhibited", ""),
                literature_raw=sections.get("literature", ""),
                lot_essay_raw=sections.get("lot essay", ""),
                condition_raw=sections.get("condition report", sections.get("condition", "")),
                image_path=image_path, collected_at_utc=utc_text(collected),
                session_start_utc=utc_text(start), cutoff_source_url=args.overview_url,
            )
            lots.append({field: csv_text(value) if isinstance(value, str) else value
                         for field, value in lot_row.items()})
            estimates.append(dict(estimate, lot_id=identity, collected_at_utc=utc_text(collected)))
            seen.add(identity)
            if len(lots) == args.limit:
                break
        write_csv(staging / "lots.csv", LOT_FIELDS, lots)
        write_csv(staging / "estimates.csv", ESTIMATE_FIELDS, estimates)
        if len(lots) < 10:
            raise PipelineError(f"Only {len(lots)} qualifying lots. Check remaining catalog links "
                                "or select another upcoming session.")
        write_log(staging / "errors.txt", log)
        if history_builder is not None:
            try:
                history = history_builder(lots, start)
                write_json(staging / HISTORY_FILE, history)
                checked_history(history, lots, start, now())
                confirm_schedule(page(fetch, args.overview_url), args.overview_url,
                                 args.lot_from, args.lot_to, start, now)
            except DeadlineError:
                raise
            except (PipelineError, OSError, requests.RequestException, UnicodeError, ValueError) as exc:
                raise HistoryReadinessError(str(exc)) from exc
        with FileLock(args.output_dir / ".collections.lock", timeout=30):
            ready_at = now() if history_builder is not None else None
            write_manifest(staging, start, ready_at)
            check_deadline(start, now)
            destination = free_name(args.output_dir, name)
            staging.rename(destination)
            if ready_at is not None:
                cleanup_expired(args.output_dir, destination, now)
    except (PipelineError, OSError, requests.RequestException, UnicodeError, Timeout) as exc:
        if not staging.exists():
            raise
        log_entry(log, args.catalog_url, "snapshot", exc)
        write_log(staging / "errors.txt", log)
        write_manifest(staging, start)
        error_type = type(exc) if isinstance(exc, (HistoryReadinessError, DeadlineError)) else PipelineError
        raise error_type(f"{exc}. Incomplete attempt: {staging}") from exc
    return destination, len(lots)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-url", help="Select a specific auction instead of searching the calendar")
    parser.add_argument("--overview-url", help="Christie's official schedule URL")
    parser.add_argument("--lot-from", type=parse_lot_number)
    parser.add_argument("--lot-to", type=parse_lot_number)
    parser.add_argument("--session-start", help="Optional cross-check of the official time in ISO 8601")
    parser.add_argument("--lot-urls-file", type=Path, help="Manual fallback, one lot URL per line")
    parser.add_argument("--output-dir", type=Path, default=Path("snapshots"))
    parser.add_argument("--limit", type=int, choices=range(10, 16), default=12)
    parser.add_argument("--skip-history", action="store_true",
                        help="Save the snapshot without downloading the five-year artist history")
    args = parser.parse_args(argv)
    if args.catalog_url is None:
        if any(value is not None for value in (args.overview_url, args.lot_from, args.lot_to,
                                               args.session_start, args.lot_urls_file)):
            parser.error("Supply --catalog-url with manual session options")
        return args
    if args.lot_from is None or args.lot_to is None or not args.lot_from <= args.lot_to:
        parser.error("Supply a positive and ordered --lot-from / --lot-to range")
    if args.overview_url is None:
        if not args.catalog_url.endswith("/browse-lots"):
            parser.error("Supply --overview-url for the official session schedule")
        args.overview_url = args.catalog_url.removesuffix("/browse-lots") + "/overview"
    try:
        if args.session_start is not None:
            args.session_start = utc_datetime(args.session_start)
        for url in (args.catalog_url, args.overview_url):
            checked_url(url, {"www.christies.com"})
    except PipelineError as exc:
        parser.error(str(exc))
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mark-used"]:
        parser = argparse.ArgumentParser(prog="load_christies.py mark-used")
        parser.add_argument("snapshot", type=Path)
        parser.add_argument("--lot-id", required=True, action="append")
        selected = parser.parse_args(argv[1:])
        try:
            archive = archive_used_lots(selected.snapshot, set(selected.lot_id))
        except (PipelineError, OSError, ValueError, csv.Error, Timeout) as exc:
            print(f"Archive stopped: {exc}", file=sys.stderr)
            return 1
        print(f"Saved used lots: {archive.resolve()}")
        return 0
    args = parse_args(argv)
    history_builder = None
    if not args.skip_history:
        if __name__ == "__main__":
            sys.modules["load_christies"] = sys.modules[__name__]
        from artist_history import build_history, fetch_public

        def history_builder(lots, start):
            print("Scanning five years of Christie’s catalogues...", file=sys.stderr, flush=True)
            return build_history(lots, fetch_public, deadline=start)

    with requests.Session() as session:
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        try:
            path, count = collect(args, lambda url: fetch_resource(session, url), history_builder=history_builder)
        except (PipelineError, OSError, requests.RequestException, UnicodeError) as exc:
            print(f"Collection stopped: {exc}", file=sys.stderr)
            return 1
    print(f"Saved {count} lots: {path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
