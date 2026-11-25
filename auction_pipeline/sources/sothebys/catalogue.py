"""Collect a bounded, pre-auction Sotheby's painting snapshot."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import requests
from bs4 import BeautifulSoup
from filelock import Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ... import common, snapshots


BASE = "https://www.sothebys.com"
HOSTS = {"www.sothebys.com", "sothebys-md.brightspotcdn.com",
         "sothebys-brightspot.s3.amazonaws.com", "cdn.sothebys.com"}
CATALOG_PATH = r"/en/buy/auction/\d{4}/[a-z0-9-]+"
CATALOG_LIMIT = 48
HISTORY_LIMIT = 3
ERRORS = (common.PipelineError, requests.RequestException, UnicodeError, OSError, Timeout)


def public_page(fetch, url, lot=False):
    common.checked_url(url, {"www.sothebys.com"})
    pattern = CATALOG_PATH + (r"/[a-z0-9-]+" if lot else "")
    if not re.fullmatch(pattern, urlsplit(url).path):
        raise common.PipelineError(f"Unexpected Sotheby's URL: {url}")
    soup = BeautifulSoup(fetch(url), "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    try:
        props = json.loads(script.string if script else "")["props"]["pageProps"]
        cache = props["apolloCache"]
        auction, = entities(cache, "Auction", "auctionId", props["auctionId"])
        record = auction
        reference_slug = auction.get("slug") or {}
        canonical = f"/en/buy/auction/{reference_slug.get('year')}/{reference_slug.get('name')}"
        if lot:
            record, = entities(cache, "LotV2", "lotId", props["lotId"])
            slug = record["slug"]
            canonical += "/" + (slug["lotSlug"] if isinstance(slug, dict) else slug)
        if canonical != urlsplit(url).path:
            raise ValueError("Canonical URL mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise common.PipelineError(f"Unrecognized Sotheby's catalogue data: {url} ({exc})") from exc
    return auction, record, cache, soup


def entities(cache, name, field=None, value=None):
    return [item for key, item in cache.items() if key.startswith(name + ":")
            and (field is None or item.get(field) == value)]


def resolve(cache, value):
    value = value or {}
    return cache.get(value["__ref"], {}) if "__ref" in value else value


def upcoming_sessions(auction, cache, now):
    if (auction.get("type") != "Live" or auction.get("state") not in {"Opened", "PreOpen", "Published"}
            or (auction.get("dates") or {}).get("closed")):
        return
    for reference in auction.get("sessions") or []:
        session = resolve(cache, reference)
        if resolve(cache, session.get("stateV2")).get("state") != "PreOpen":
            continue
        start = common.utc_datetime(session.get("scheduledOpeningDate"))
        bounds = session.get("lotRange") or {}
        low = common.parse_lot_number(str(bounds.get("fromLotDisplayNumber") or ""))
        high = common.parse_lot_number(str(bounds.get("toLotDisplayNumber") or ""))
        if now < start and low and high and low <= high and session.get("sessionId") is not None:
            yield start, low, high, session["sessionId"]


def auction_links(html):
    for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
        url = urlsplit(urljoin(BASE, anchor["href"]))
        if url.hostname == "www.sothebys.com" and re.fullmatch(CATALOG_PATH, url.path):
            description = url.path.rsplit("/", 1)[-1].replace("-", " ")
            if common.ART_AUCTION_TITLE.search(description) or re.search(
                    r"\b(?:modern|contemporary) (?:day|evening|discoveries)\b", description):
                yield BASE + url.path


def number(record):
    return common.parse_lot_number(str((record.get("lotNumber") or {}).get("lotDisplayNumber") or ""))


def card_records(cache, auction):
    return [row for row in entities(cache, "LotCard")
            if (row.get("auction") or {}).get("auctionId") == auction["auctionId"]][:CATALOG_LIMIT]


def card_url(catalog, card):
    slug = (card.get("slug") or {}).get("lotSlug") or ""
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise common.PipelineError("Missing or invalid Sotheby's lot slug")
    return urlsplit(catalog)._replace(query="", fragment="").geturl() + "/" + slug


def current_estimate(record, auction, soup):
    estimate = record.get("estimateV2") or {}
    currency = auction.get("currencyV2") or ""
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise common.PipelineError("Missing auction currency")
    low = (estimate.get("lowEstimate") or {}).get("amount")
    high = (estimate.get("highEstimate") or {}).get("amount")
    pattern = re.compile(rf"\s*[\d,. ]+\s*[-–—−]\s*[\d,. ]+\s+{currency}\s*")
    displayed = list(dict.fromkeys(str(item).strip() for item in soup.find_all(string=pattern.fullmatch)
                                  if item.parent.name not in {"script", "style"}))
    if len(displayed) != 1:
        raise common.PipelineError("Missing or ambiguous public lot estimate")
    return common.estimate_data({"estimate_txt": displayed[0], "estimate_low": low,
                                 "estimate_high": high}, {"currency_txt": currency})


def image_urls(record):
    media = next((item for key, item in record.items() if key.startswith("media(")), None) or {}
    images = media.get("images") or []
    for rendition in sorted((images[0].get("renditions") or []) if images else [],
                            key=lambda image: image.get("width") or 0):
        url = rendition.get("url")
        if url and 600 <= (rendition.get("width") or 0) <= 2048:
            common.checked_url(url, HOSTS)
            yield url


def lot_rows(args, fetch, auction, cache, start, session_id, now, staging, log):
    if args.lot_urls_file:
        candidates = [(url.strip(), None) for url in args.lot_urls_file.read_text(encoding="utf-8-sig")
                      .splitlines() if url.strip()][:CATALOG_LIMIT]
    else:
        candidates = [(card_url(args.catalog_url, card), card) for card in sorted(
            card_records(cache, auction), key=lambda card: number(card) or (0, ""))
            if card.get("sessionId") == session_id
            and common.in_lot_range(number(card), args.lot_from, args.lot_to)]
    lots, estimates, seen = [], [], set()
    for url, preview in candidates:
        common.check_deadline(start, now)
        try:
            sale, record, lot_cache, soup = public_page(fetch, url, lot=True)
            identity = str(UUID(record.get("lotId") or ""))
            session = resolve(lot_cache, record.get("session"))
            bid = resolve(lot_cache, record.get("bidState"))
            withdrawn = resolve(lot_cache, record.get("withdrawnState"))
            lot_number = number(record)
            if (sale["auctionId"] != auction["auctionId"] or identity in seen
                    or not common.in_lot_range(lot_number, args.lot_from, args.lot_to)
                    or (preview is not None and (identity != preview.get("lotId") or lot_number != number(preview)))
                    or session.get("sessionId") != session_id
                    or common.utc_datetime(session.get("scheduledOpeningDate")) != start
                    or resolve(lot_cache, session.get("stateV2")).get("state") != "PreOpen"
                    or sale.get("state") == "Closed" or (sale.get("dates") or {}).get("closed")
                    or bid.get("isClosed") is not False
                    or (bid.get("sold") or {}).get("isSold") is not False
                    or ((bid.get("sold") or {}).get("premiums") or {}).get("finalPriceV2") is not None
                    or withdrawn.get("state") != "NotAffected"):
                raise common.PipelineError("Lot identity, session or pre-auction status could not be verified")
            details = common.plain_text(record.get("description"))
            medium = common.painting_medium(details)
            artist = common.plain_text(record.get("creatorsDisplayTitle"))
            title = common.plain_text(record.get("title"))
            if not (medium and artist and title):
                raise common.PipelineError("No qualifying painting with artist and title")
            estimate = current_estimate(record, sale, soup)
            extension, data = common.download_photo(fetch, list(image_urls(record)), HOSTS)
        except (*ERRORS, ValueError) as exc:
            snapshots.log_entry(log, url, "lot", exc)
            continue
        collected = common.utc_text(now())
        common.check_deadline(start, lambda: common.utc_datetime(collected))
        image_path = f"images/{identity}{extension}"
        (staging / image_path).write_bytes(data)
        row = {
            "lot_id": identity, "lot_number": common.lot_label(lot_number), "sale_id": sale["auctionId"],
            "lot_url": url, "artist": artist, "title": title, "medium_raw": medium,
            "details_raw": details, "creation_date_raw": common.matching_line(details, common.CREATION),
            "dimensions_raw": common.dimensions_text(details), "image_path": image_path,
            "collected_at_utc": collected, "session_start_utc": common.utc_text(start),
            "cutoff_source_url": args.overview_url,
        }
        for field, source in (("provenance_raw", "provenance"), ("exhibited_raw", "exhibition"),
                              ("literature_raw", "literature"), ("lot_essay_raw", "catalogueNote"),
                              ("condition_raw", "condition")):
            value = record.get(source)
            row[field] = common.plain_text(value) if isinstance(value, str) else ""
        lots.append({key: common.csv_text(value) for key, value in row.items()})
        estimates.append(dict(estimate, lot_id=identity, collected_at_utc=collected))
        seen.add(identity)
        if len(lots) == args.limit:
            break
    return lots, estimates


def collect(args, fetch, now=common.utc_now):
    urls = ([args.catalog_url] if args.catalog_url else list(dict.fromkeys(
        auction_links(fetch(BASE + "/en/calendar?locale=en"))))[:5])
    failures = []
    for url in urls:
        try:
            auction, _, cache, _ = public_page(fetch, url)
            overview = args.overview_url or url
            schedule, _, schedule_cache, _ = (auction, auction, cache, None) if overview == url else public_page(fetch, overview)
            if schedule["auctionId"] != auction["auctionId"]:
                raise common.PipelineError("Catalogue and schedule are different auctions")
            sessions = sorted(upcoming_sessions(schedule, schedule_cache, now()))
            for start, low, high, session_id in sessions:
                lower, upper = args.lot_from or low, args.lot_to or high
                if not (common.in_lot_range(lower, low, high) and common.in_lot_range(upper, low, high)):
                    continue
                if args.session_start and start != args.session_start:
                    continue
                selection = argparse.Namespace(**(vars(args) | {"catalog_url": url, "overview_url": overview,
                                                                 "lot_from": lower, "lot_to": upper}))

                def recheck():
                    refreshed, _, refreshed_cache, _ = public_page(fetch, overview)
                    if (refreshed["auctionId"] != auction["auctionId"]
                            or (start, low, high, session_id) not in upcoming_sessions(refreshed, refreshed_cache, now())):
                        raise common.AuctionStatusError("Sotheby's session changed during collection")

                history_builder = None
                if not args.skip_history:
                    from .results import build_history

                    history_builder = lambda lots, deadline: build_history(
                        lots, fetch, deadline, now, args.history_catalog_url)
                return snapshots.publish_snapshot(selection, {"title": auction["title"], "source": "sothebys",
                    "sale_number": auction.get("sapSaleNumber") or auction["auctionId"]},
                    start, now, history_builder, lambda staging, log: lot_rows(
                        selection, fetch, auction, cache, start, session_id, now, staging, log), recheck)
        except common.HistoryReadinessError:
            raise
        except ERRORS as exc:
            failures.append(str(exc))
    raise common.PipelineError(f"No eligible Sotheby's pilot session. {' | '.join(failures)}")


def parse_args(argv):
    def lot_bound(value):
        bound = common.parse_lot_number(value)
        if bound is None:
            raise argparse.ArgumentTypeError("Expected a positive lot number, optionally with a letter suffix")
        return bound

    def session_time(value):
        try:
            return common.utc_datetime(value)
        except common.PipelineError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-url")
    parser.add_argument("--overview-url")
    parser.add_argument("--lot-from", type=lot_bound)
    parser.add_argument("--lot-to", type=lot_bound)
    parser.add_argument("--session-start", type=session_time)
    parser.add_argument("--lot-urls-file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("snapshots/sothebys"))
    parser.add_argument("--limit", type=int, choices=range(10, 16), default=12)
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--history-catalog-url", action="append", default=[])
    args = parser.parse_args(argv)
    if args.lot_from and args.lot_to and args.lot_from > args.lot_to:
        parser.error("Lot range must be in ascending order")
    if (args.lot_urls_file or args.overview_url) and not args.catalog_url:
        parser.error("--lot-urls-file and --overview-url require --catalog-url")
    if len(args.history_catalog_url) > HISTORY_LIMIT:
        parser.error(f"The pilot supports at most {HISTORY_LIMIT} historical catalogues")
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mark-used"]:
        return snapshots.mark_used_main(argv[1:], "load_sothebys.py mark-used")
    try:
        args = parse_args(argv)
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))))
            session.headers["Accept-Language"] = "en-US,en;q=0.9"
            path, count = collect(args, lambda url: common.fetch_resource(session, url, HOSTS))
    except ERRORS as exc:
        print(f"Collection stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {count} lots: {path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
