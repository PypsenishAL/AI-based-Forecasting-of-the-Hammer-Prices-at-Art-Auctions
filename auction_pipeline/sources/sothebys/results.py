"""Sotheby's bounded historical results adapter."""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from ... import common, history as artist_history
from .catalogue import BASE, CATALOG_LIMIT, ERRORS, HISTORY_LIMIT, auction_links, card_records, card_url, public_page, resolve


def public_result(bid, currency):
    sold = bid.get("sold") or {}
    if sold.get("__typename") != "ResultVisible" or type(sold.get("isSold")) is not bool:
        return None
    if not sold["isSold"]:
        return None, None
    final = (sold.get("premiums") or {}).get("finalPriceV2") or {}
    hammer = sold.get("hammerPriceV2") or {}
    try:
        price = Decimal(str(final.get("amount")))
        if not price.is_finite() or price <= 0 or final.get("currency") != currency:
            return None
        value = Decimal(str(hammer.get("amount"))) if hammer.get("currency") == currency else None
        return format(price, "f"), (format(value, "f") if value and value.is_finite() and 0 < value <= price else None)
    except InvalidOperation:
        return None


def historical_catalogue(url, fetch, names, excluded, start, cutoff, deadline, now):
    auction, _, cache, _ = public_page(fetch, url)
    if auction.get("state") != "Closed":
        raise common.PipelineError("Historical catalogue is not closed")
    closed = common.utc_datetime((auction.get("dates") or {}).get("closed"))
    if not start <= closed < cutoff:
        raise common.PipelineError("Historical catalogue outside the five-year window")
    currency = auction.get("currencyV2") or ""
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise common.PipelineError("Missing historical auction currency")
    cards = card_records(cache, auction)
    if not cards:
        raise common.PipelineError("No public historical lot cards")
    errors, sales = [], []
    if all(public_result(resolve(cache, row.get("bidState")), currency) is None for row in cards):
        return [], [{"url": url, "error": "Results hidden or unpublished in the public catalogue"}]
    for card in cards:
        common.check_deadline(deadline, now)
        name, _ = artist_history.artist_identity(card.get("creatorsDisplayTitle"))
        if name not in names or card.get("lotId") in excluded:
            continue
        lot_url = card_url(url, card)
        try:
            sale, record, lot_cache, _ = public_page(fetch, lot_url, lot=True)
            if sale["auctionId"] != auction["auctionId"] or record["lotId"] != card["lotId"]:
                raise common.PipelineError("Historical lot identity does not match catalogue")
            details = common.plain_text(record.get("description"))
            medium = common.painting_category(details)
            if not medium:
                continue
            result = public_result(resolve(lot_cache, record.get("bidState")), currency)
            if result is None:
                raise common.PipelineError("Historical sale result unavailable")
            price, hammer = result
            if price is not None:
                identity = artist_history.artist_identity(record.get("creatorsDisplayTitle"))
                sales.append((identity, {
                    "historical_lot_id": record["lotId"], "title": common.plain_text(record.get("title")),
                    "medium": medium, "dimensions_raw": common.dimensions_text(details),
                    "sale_date": closed.date().isoformat(), "sold_at_utc": None, "currency": currency,
                    "price": price, "price_raw": f"{currency} {price}", "hammer_price": hammer,
                    "lot_url": lot_url, "catalogue_url": url, "sale_format": auction["type"].lower(),
                }))
        except ERRORS as exc:
            errors.append({"url": lot_url, "error": str(exc)})
    return sales, errors


def build_history(lots, fetch, deadline, now, archives):
    cutoff = min(common.utc_datetime(row["collected_at_utc"]) for row in lots)
    start = artist_history.five_years_before(cutoff)
    artists = {artist_history.artist_identity(row["artist"])[0] for row in lots}
    excluded = {row["lot_id"] for row in lots}
    errors = []
    if not archives:
        try:
            archives = list(dict.fromkeys(auction_links(fetch(BASE + "/en/results?locale=en"))))[:HISTORY_LIMIT]
        except ERRORS as exc:
            errors.append({"url": BASE + "/en/results", "error": str(exc)})
    by_artist = defaultdict(dict)
    for url in archives:
        common.check_deadline(deadline, now)
        try:
            sales, failures = historical_catalogue(url, fetch, artists, excluded, start, cutoff, deadline, now)
            errors.extend(failures)
            for (name, dates), sale in sales:
                by_artist[name][("sothebys", sale["historical_lot_id"])] = dates, sale
        except common.DeadlineError:
            raise
        except ERRORS as exc:
            errors.append({"url": url, "error": str(exc)})
    common.check_deadline(deadline, now)
    report = artist_history.summarize_history(lots, by_artist, start, cutoff, now(), deadline,
                                              0, len(archives), errors, "sothebys_public_catalogue_pilot")
    report["coverage_limits"] = {"catalogues": HISTORY_LIMIT, "lots_per_catalogue": CATALOG_LIMIT}
    return report
