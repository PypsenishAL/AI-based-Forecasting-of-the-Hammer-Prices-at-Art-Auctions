"""Christie's current catalogue and session adapter."""

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...common import (
    ART_AUCTION_TITLE, CREATION, AuctionStatusError, HistoryReadinessError, PipelineError,
    check_deadline, checked_url, csv_text, dimensions_text, estimate_data, in_lot_range,
    lot_label, matching_line, painting_medium, parse_lot_number, plain_text,
    section_text, utc_datetime, utc_now, utc_text,
)
from ...common import download_photo as shared_download_photo
from ...common import fetch_resource as shared_fetch_resource
from ...snapshots import log_entry, mark_used_main, publish_snapshot


BASE = "https://www.christies.com"
CALENDAR = BASE + "/en/calendar"
PAGINATION = BASE + "/api/discoverywebsite/auctionpages/lotsearch"
IMAGE_HOSTS = {"www.christies.com", "www.christies-media.com",
               "onlineonly.christies.com", "cdn.christies.com"}
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
    def recheck():
        confirm_schedule(page(fetch, args.overview_url), args.overview_url,
                         args.lot_from, args.lot_to, start, now)

    return publish_snapshot(args, auction, start, now, history_builder,
                            lambda staging, log: collect_christies_lots(
                                args, fetch, auction, start, now, staging, log), recheck)


def collect_christies_lots(args, fetch, auction, start, now, staging, log):
    lots, estimates, seen = [], [], set()
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
    return lots, estimates


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
        return mark_used_main(argv[1:], "load_christies.py mark-used")
    args = parse_args(argv)
    history_builder = None
    if not args.skip_history:
        from .results import build_history, fetch_public

        def collect_history(lots, start):
            print("Scanning five years of Christie’s catalogues...", file=sys.stderr, flush=True)
            return build_history(lots, fetch_public, deadline=start)

        history_builder = collect_history

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


def fetch_resource(session, url, hosts=IMAGE_HOSTS):
    return shared_fetch_resource(session, url, hosts)


def download_photo(fetch, urls, hosts=IMAGE_HOSTS):
    return shared_download_photo(fetch, urls, hosts)
