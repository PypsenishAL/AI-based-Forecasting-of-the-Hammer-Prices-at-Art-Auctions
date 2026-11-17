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
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE = "https://www.christies.com"
CALENDAR = BASE + "/en/calendar"
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
MEDIUM = re.compile(
    r"(?:oil|acrylic|tempera) on (?:(?:unprimed |primed |raw )?(?:canvas|linen)"
    r"|(?:(?:wood|wooden|artist['’]s) )?panel|masonite)"
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


class PipelineError(Exception):
    """The source or input does not support a verified snapshot."""


class AuctionStatusError(PipelineError):
    """The source cannot confirm that the auction is still upcoming."""


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
        raise PipelineError(f"Session deadline missed ({utc_text(start)})")


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


def confirm_upcoming(status, source):
    if any(status.get(key) is not False for key in ("is_in_progress", "is_auction_over")):
        raise AuctionStatusError(f"Upcoming auction status is unconfirmed or conflicts with time: {source}")


def live_auction(html, source):
    auction = component(html, "auction")["data"]
    status = auction.get("auction_status") or {}
    if auction.get("event_type") != "Sale":
        raise PipelineError("This loader requires a live auction")
    confirm_upcoming(status, source)
    if not all(re.fullmatch(r"[A-Za-z0-9]+", auction.get(key) or "")
               for key in ("sale_id", "sale_number", "sale_room_code")):
        raise PipelineError("Missing auction identifiers")
    return auction, status


def numeric_sessions(status):
    for session in status.get("sessions") or []:
        bounds = re.fullmatch(r"Lots?\s+(\d+)(?:\s*[-–]\s*(\d+))?",
                              session.get("lot_txt") or "", re.I)
        if bounds:
            yield int(bounds[1]), int(bounds[2] or bounds[1]), session


def confirm_schedule(html, source, lower, upper, expected):
    auction, status = live_auction(html, source)
    if status.get("sessions") == []:
        official = utc_datetime(status.get("start_date"))
    else:
        matching = [session for low, high, session in numeric_sessions(status)
                    if low <= lower <= upper <= high]
        if len(matching) != 1:
            raise PipelineError("Official schedule does not identify a single session for the lot range")
        official = utc_datetime(matching[0].get("date_time_universal"))
    if expected is not None and official != expected:
        raise PipelineError(f"Session start conflicts with official schedule ({utc_text(official)})")
    return auction, official


def plain_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    return section_text(soup)


def section_text(node):
    for tag in node.find_all(["script", "style"]):
        tag.decompose()
    for tag in node.find_all("br"):
        tag.replace_with("\n")
    for tag in node.find_all(["p", "li"]):
        tag.insert_after("\n")
    return "\n".join(line for raw in node.get_text().splitlines() if (line := " ".join(raw.split())))


def painting_medium(text):
    matches = [line for line in text.splitlines() if MEDIUM.fullmatch(line)]
    return matches[0] if len(matches) == 1 else ""


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
    value = str(row.get("lot_id_txt", "")).strip()
    return int(value) if re.fullmatch(r"[0-9]+", value) else None


def reject_realised(row):
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
    if any(row.get(key) for key in ("lot_withdrawn", "is_unsold", "is_auction_over", "is_in_progress")):
        raise PipelineError("Lot is withdrawn, unsold, closed or in progress")


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
            confirm_upcoming(sale, f"lot {identity}")
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
    return (f"{title} - {auction['sale_number']} - lots {args.lot_from}-{args.lot_to}"
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


def write_manifest(folder, start):
    payload = {"version": 1, "session_start_utc": utc_text(start), "files": hash_files(folder)}
    (folder / MANIFEST).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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


def archive_used_lots(snapshot, lot_ids):
    if not lot_ids:
        raise PipelineError("Supply at least one --lot-id")
    root = snapshot.absolute().parent
    with FileLock(root / ".collections.lock", timeout=30):
        if not snapshot.is_dir() or is_link(snapshot) or snapshot.name.endswith(".incomplete"):
            raise PipelineError("Select a completed snapshot")
        hashes = hash_files(snapshot)
        lots = [row for row in read_csv(snapshot / "lots.csv", LOT_FIELDS) if row["lot_id"] in lot_ids]
        estimates = [row for row in read_csv(snapshot / "estimates.csv", ESTIMATE_FIELDS)
                     if row["lot_id"] in lot_ids]
        for rows in (lots, estimates):
            if len(rows) != len(lot_ids) or {row["lot_id"] for row in rows} != lot_ids:
                raise PipelineError("Each requested lot must occur once in both CSV files")
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
        (staging / "images").mkdir(parents=True)
        try:
            write_csv(staging / "lots.csv", LOT_FIELDS, lots)
            write_csv(staging / "estimates.csv", ESTIMATE_FIELDS, estimates)
            for row in lots:
                shutil.copyfile(snapshot / row["image_path"], staging / row["image_path"])
            if "errors.txt" in hashes:
                shutil.copyfile(snapshot / "errors.txt", staging / "errors.txt")
            if HISTORY_FILE in hashes:
                history = json.loads((snapshot / HISTORY_FILE).read_text(encoding="utf-8"))
                history["lots"] = [row for row in history["lots"] if row["lot_id"] in lot_ids]
                (staging / HISTORY_FILE).write_text(
                    json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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


def candidate_rows(args, fetch, auction, log):
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
                                       args.lot_to, args.session_start)
    if any(catalog_sale[key] != auction[key] for key in ("sale_id", "sale_number", "sale_room_code")):
        raise PipelineError("Catalog and schedule belong to different auctions")
    rows = component(html, "lots")["data"].get("lots")
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


def discover_and_collect(args, fetch, now):
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
                    if start > now() and high - low >= 9:
                        upcoming.append((start, catalog, overview, low, high))
            except fetch_errors as exc:
                last_error = exc
        catalogs = {}
        for start, catalog, overview, low, high in sorted(upcoming, key=lambda item: item[0]):
            try:
                check_deadline(start, now)
                if catalog not in catalogs:
                    preview = page(fetch, catalog)
                    rows = component(preview, "lots")["data"].get("lots") or []
                    catalogs[catalog] = [lot_number(row) for row in rows
                                         if painting_medium(plain_text(row.get("description_txt")))]
                numbers = {number for number in catalogs[catalog] if number is not None}
                if not numbers:
                    continue
                if low is None:
                    low, high = min(numbers), max(numbers)
                if sum(low <= number <= high for number in numbers) < 10:
                    continue
                selection = argparse.Namespace(**(vars(args) | {
                    "catalog_url": catalog, "overview_url": overview,
                    "lot_from": low, "lot_to": high, "session_start": start,
                }))
                return collect(selection, fetch, now)
            except fetch_errors as exc:
                last_error = exc
                print(f"Skipping session {overview} ({utc_text(start)}): {exc}", file=sys.stderr)
    message = "No upcoming live painting session with at least 10 qualifying lots on the published catalog pages"
    if last_error is not None:
        message += f". Last source error: {last_error}"
    raise PipelineError(message)


def collect(args, fetch, now=utc_now):
    if args.catalog_url is None:
        return discover_and_collect(args, fetch, now)
    if args.session_start is not None:
        check_deadline(args.session_start, now)
    auction, start = confirm_schedule(page(fetch, args.overview_url), args.overview_url,
                                      args.lot_from, args.lot_to, args.session_start)
    check_deadline(start, now)
    args = argparse.Namespace(**(vars(args) | {"session_start": start}))
    log = []
    name = collection_name(auction, args, now())
    staging = args.output_dir / f"{name} ({uuid4().hex[:8]}).incomplete"
    (staging / "images").mkdir(parents=True)
    lots, estimates, seen = [], [], set()
    try:
        candidates = candidate_rows(args, fetch, auction, log)
        candidates.sort(key=lambda candidate: lot_number(candidate[0]) or 0)
        for row, url, html, labels in candidates:
            check_deadline(start, now)
            number = lot_number(row)
            stage = "catalog"
            try:
                if number is None or not args.lot_from <= number <= args.lot_to:
                    raise PipelineError("Lot number outside the selected session range")
                identity = lot_identity(url)
                if identity != str(row.get("object_id")):
                    raise PipelineError("Catalog object ID does not match lot URL")
                if identity in seen:
                    raise PipelineError("Duplicate lot object ID")
                reject_realised(row)
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
                lot_id=identity, lot_number=number, sale_id=auction["sale_id"],
                lot_url=BASE + f"/en/lot/lot-{identity}", artist=artist, title=title,
                creation_date_raw=matching_line(details, CREATION), medium_raw=medium,
                dimensions_raw="\n".join(line for line in details.splitlines() if DIMENSIONS.search(line)),
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
        with FileLock(args.output_dir / ".collections.lock", timeout=30):
            write_manifest(staging, start)
            check_deadline(start, now)
            destination = free_name(args.output_dir, name)
            staging.rename(destination)
            cleanup_expired(args.output_dir, destination, now)
    except (PipelineError, OSError, requests.RequestException, UnicodeError, Timeout) as exc:
        if not staging.exists():
            raise
        log_entry(log, args.catalog_url, "snapshot", exc)
        write_log(staging / "errors.txt", log)
        write_manifest(staging, start)
        raise PipelineError(f"{exc}. Incomplete attempt: {staging}") from exc
    return destination, len(lots)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-url", help="Select a specific auction instead of searching the calendar")
    parser.add_argument("--overview-url", help="Christie's official schedule URL")
    parser.add_argument("--lot-from", type=int)
    parser.add_argument("--lot-to", type=int)
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
    if args.lot_from is None or args.lot_to is None or not 0 < args.lot_from <= args.lot_to:
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
    with requests.Session() as session:
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        try:
            path, count = collect(args, lambda url: fetch_resource(session, url))
        except (PipelineError, OSError, requests.RequestException, UnicodeError) as exc:
            print(f"Collection stopped: {exc}", file=sys.stderr)
            return 1
    print(f"Saved {count} lots: {path.resolve()}")
    if not args.skip_history:
        from artist_history import main as collect_history

        return collect_history([str(path)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
