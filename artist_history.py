"""Build an auditable five-year market brief for Christie's snapshot lots."""

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from filelock import FileLock, Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import load_christies as loader


RESULTS = loader.BASE + "/en/results"
ONLINE = "https://onlineonly.christies.com"
CATALOGUE_TITLE = re.compile(
    r"paintings?|old masters|contemporary|modern|impressionis|post.war|(?:20th|21st|19th) century"
    r"|art (?:américain|européen|d'asie)|american art|british art|latin american art"
    r"|(?:chinese|indian|african|australian|canadian|irish|swiss|belgian) art|first open", re.I,
)
NON_PAINTING_CATALOGUE = re.compile(r"\b(?:prints|multiples|works on paper|photographs|sculpture)\s*$", re.I)
LIFESPAN = re.compile(r"\s*\((?P<dates>(?:b(?:orn)?\.?\s*)?\d{4}(?:\s*[-–—]\s*\d{4})?)\)\s*$", re.I)
SOURCE_ERRORS = (loader.PipelineError, requests.RequestException, UnicodeError, ValueError)


def five_years_before(cutoff):
    try:
        return cutoff.replace(year=cutoff.year - 5)
    except ValueError:
        return cutoff.replace(year=cutoff.year - 5, day=28)


def archive_months(start, cutoff):
    for index in range(start.year * 12 + start.month - 1, cutoff.year * 12 + cutoff.month):
        year, month = divmod(index, 12)
        yield f"{RESULTS}/{year}/{month + 1:02d}"


def artist_identity(value):
    name = unicodedata.normalize("NFKC", loader.plain_text(value)).casefold()
    suffix = LIFESPAN.search(name)
    dates = tuple(re.findall(r"\d{4}", suffix["dates"])) if suffix else ()
    return " ".join(name[:suffix.start() if suffix else len(name)].split()), dates


def painting_technique(description):
    return loader.painting_category(loader.plain_text(description))


def area_cm2(dimensions):
    pattern = r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*cm\b"
    lines = dimensions.replace(r"\n", "\n").splitlines()
    measurements = [line for line in lines if not re.match(r"\s*(?:overall|frame[d]?)\b", line, re.I)]
    matches = [match for line in measurements for match in re.finditer(pattern, line, re.I)]
    if len(matches) != 1:
        return None
    height, width = map(Decimal, matches[0].groups())
    return height * width if height > 0 and width > 0 else None


def verified_hammer(row, currency, realised):
    raw = loader.plain_text(row.get("hammer_price_txt") or "")
    match = re.fullmatch(r"(?:Hammer price:?\s*)?([A-Z]{3})\s+([0-9][0-9,]*(?:\.\d+)?)", raw)
    if not match or match[1] != currency:
        return None
    value = Decimal(match[2].replace(",", ""))
    try:
        structured = row.get("hammer_price")
        if structured is not None and Decimal(str(structured).replace(",", "")) != value:
            return None
    except InvalidOperation:
        return None
    return format(value, "f") if 0 < value <= realised else None


def archive_catalogues(html):
    soup = BeautifulSoup(html, "html.parser")
    if not soup.find("h1", string=re.compile(r"auction results", re.I)):
        raise loader.PipelineError("Missing Christie’s auction results page")
    for link in soup.select('h3 a[data-qa="auction-card-primary-link"][href]'):
        title = link.get_text(" ", strip=True)
        if not CATALOGUE_TITLE.search(title) or NON_PAINTING_CATALOGUE.search(title):
            continue
        url = urlsplit(urljoin(RESULTS, link["href"]))
        if url.scheme == "https" and url.netloc == "www.christies.com" and re.fullmatch(
                r"/en/auction/auction-\d+(?:-[a-z]+)?/?", url.path, re.I):
            yield loader.BASE + url.path.rstrip("/") + "/browse-lots"
        if url.scheme == "https" and url.netloc == "www.christies.com" and url.path == "/nav/onlineonly/sso":
            sale_id = parse_qs(url.query).get("SaleID", [""])[0]
            if re.fullmatch(r"[0-9]+", sale_id):
                yield f"{loader.BASE}/en/auction/auction-{sale_id}/browse-lots"


def catalogue_blocks(html):
    packed = re.search(r"\bwindow\.chrComponents\s*=\s*{", html)
    if packed:
        components, _ = json.JSONDecoder().raw_decode(html[packed.end() - 1:])
        if "auction" in components and "lots" in components:
            return components["auction"]["data"], components["lots"]
    return loader.component(html, "auction")["data"], loader.component(html, "lots")


def online_lot_url(row):
    url = urlsplit(urljoin(ONLINE, row.get("url") or ""))
    identity = str(row.get("object_id") or "")
    if (url.scheme == "https" and url.netloc == "onlineonly.christies.com"
            and re.fullmatch(rf"/s/[\w-]+/[\w-]+/{identity}/?", url.path)):
        return ONLINE + url.path
    raise loader.PipelineError("Invalid online historical lot URL")


def online_catalogue_tail(data, total, rows, fetch):
    sale_id = str((data.get("lot_search_api_endpoint") or {}).get("parameters", {}).get("saleid") or "")
    last_page = data.get("total_pages")
    if not re.fullmatch(r"[0-9]+", sale_id) or type(last_page) is not int or not 2 <= last_page <= 100:
        raise loader.PipelineError("Invalid online catalogue pagination")
    slug = urlsplit(online_lot_url(rows[0])).path.split("/")[2]
    url = f"{ONLINE}/s/{slug}/lots/{sale_id}?page={last_page}"
    html = fetch(url).decode("utf-8-sig")
    _, block = catalogue_blocks(html)
    full_page = block["data"].get("lots")
    if (not isinstance(full_page, list) or any(not isinstance(row, dict) for row in full_page)
            or len(full_page) != total or block["data"].get("total_hits_filtered") != total
            or {row["object_id"] for row in rows} - {row.get("object_id") for row in full_page}):
        raise loader.PipelineError("Incomplete online catalogue pages")
    return full_page


def catalogue_sales(url, fetch, artists, excluded, start, cutoff):
    html = loader.page(fetch, url)
    auction, block = catalogue_blocks(html)
    status = auction.get("auction_status") or {}
    online = auction.get("event_type") == "OnlineSale"
    if (auction.get("event_type") not in ("Sale", "OnlineSale") or status.get("is_auction_over") is not True
            or status.get("is_in_progress") is not False):
        return [], []
    auction_start = loader.utc_datetime(status.get("start_date"))
    auction_day = auction_start.date()
    if auction_start >= cutoff or (not online and auction_day >= cutoff.date()):
        return [], []
    data = block["data"]
    rows = data.get("lots")
    total = data.get("total_hits_filtered")
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or type(total) is not int
            or total < len(rows) or (total and not rows)):
        raise loader.PipelineError("Invalid historical catalogue list or pagination")
    currency_default = (block.get("labels") or {}).get("currency_txt", "")
    errors = []
    if online and total > len(rows):
        try:
            rows = online_catalogue_tail(data, total, rows, fetch)
        except SOURCE_ERRORS as exc:
            errors.append({"url": url, "error": str(exc)})
    if not online:
        rows, errors = loader.live_catalogue_rows(data, url, fetch)
    if online and len({row.get("object_id") for row in rows}) != total:
        errors.append({"url": url, "error": "Catalogue unique lot count differs from reported total"})
    observations = []
    for row in rows:
        name, dates = artist_identity(row.get("title_primary_txt") or "")
        identity = str(row.get("object_id") or "")
        if (name not in artists or not re.fullmatch(r"[0-9]+", identity) or (not online and identity in excluded)
                or any(row.get(key) for key in ("lot_withdrawn", "is_unsold", "price_on_request"))):
            continue
        try:
            if online:
                sold_at = loader.utc_datetime(row.get("end_date"))
                date = sold_at.date()
                in_window = start <= sold_at < cutoff
                lot_url = online_lot_url(row)
            else:
                date = max(auction_day, loader.utc_datetime(row["start_date"]).date()) if row.get("start_date") else auction_day
                in_window = start.date() < date < cutoff.date()
                lot_url = f"{loader.BASE}/en/lot/lot-{identity}"
            price = Decimal(str(row.get("price_realised") or "").replace(",", ""))
        except (InvalidOperation, loader.PipelineError):
            continue
        if not (in_window and price.is_finite() and price > 0):
            continue
        description = loader.plain_text(row.get("description_txt") or "")
        technique = loader.painting_category(description)
        raw_price = loader.plain_text(row.get("price_realised_txt") or "")
        currencies = set(re.findall(r"\b[A-Z]{3}\b", raw_price))
        currency = next(iter(currencies)) if len(currencies) == 1 else currency_default if not currencies else ""
        if not technique or not re.fullmatch(r"[A-Z]{3}", currency or ""):
            continue
        observations.append((name, dates, dict(
            historical_lot_id=identity, title=loader.plain_text(row.get("title_secondary_txt") or ""),
            medium=technique, sale_date=date.isoformat(), currency=currency,
            sold_at_utc=loader.utc_text(sold_at) if online else None,
            price=format(price.normalize(), "f"), price_raw=raw_price,
            lot_url=lot_url, catalogue_url=url, sale_format="online" if online else "live",
            dimensions_raw=loader.dimensions_text(description),
            hammer_price=verified_hammer(row, currency, price),
        )))
    return observations, errors


def stats(sales):
    values = sorted(Decimal(sale["price"]) for sale in sales)

    def quantile(numerator):
        index, remainder = divmod((len(values) - 1) * numerator, 4)
        return values[index] if not remainder else (
            values[index] + (values[index + 1] - values[index]) * Decimal(remainder) / 4)

    summary = {"q25": quantile(1), "median": quantile(2), "q75": quantile(3),
               "mean": sum(values) / len(values)}
    return {"count": len(values), **{key: format(value.normalize(), "f") for key, value in summary.items()}}


def build_history(lots, fetch, now=loader.utc_now, workers=4, deadline=None):
    if not lots:
        raise loader.PipelineError("Snapshot contains no lots")
    cutoff = min(loader.utc_datetime(row["collected_at_utc"]) for row in lots)
    if deadline is not None:
        loader.check_deadline(deadline, now)
    start = five_years_before(cutoff)
    artists = {artist_identity(row["artist"])[0] for row in lots}
    excluded = {row["lot_id"] for row in lots}
    months = list(archive_months(start, cutoff))

    def find_catalogues(url):
        if deadline is not None and now() >= deadline:
            return [], []
        try:
            return list(archive_catalogues(loader.page(fetch, url))), []
        except SOURCE_ERRORS as exc:
            return [], [{"url": url, "error": str(exc)}]

    def read_catalogue(url):
        if deadline is not None and now() >= deadline:
            return [], []
        try:
            return catalogue_sales(url, fetch, artists, excluded, start, cutoff)
        except SOURCE_ERRORS as exc:
            return [], [{"url": url, "error": str(exc)}]

    errors = []
    by_artist = defaultdict(dict)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        catalogues = {}
        for links, problems in pool.map(find_catalogues, months):
            errors.extend(problems)
            catalogues.update(dict.fromkeys(links))
        for sales, problems in pool.map(read_catalogue, catalogues):
            errors.extend(problems)
            for name, dates, sale in sales:
                key = (sale["sale_format"], sale["historical_lot_id"])
                by_artist[name].setdefault(key, (dates, sale))
    if deadline is not None:
        loader.check_deadline(deadline, now)
    return summarize_history(lots, by_artist, start, cutoff, now(), deadline,
                             len(months), len(catalogues), errors,
                             "christies_public_painting_catalogues")


def summarize_history(lots, by_artist, start, cutoff, generated, deadline,
                      months, catalogues, errors, source_scope):
    briefs = []
    for row in lots:
        name, dates = artist_identity(row["artist"])
        related = [sale for match, sale in by_artist.get(name, {}).values()
                   if all(left == right for left, right in zip(dates, match))]
        related.sort(key=lambda sale: (sale["sale_date"], sale["historical_lot_id"]))
        currencies = defaultdict(list)
        comparable = defaultdict(list)
        target_area = area_cm2(row.get("dimensions_raw", ""))
        target_medium = loader.PAINTING.fullmatch(row.get("medium_raw", ""))
        for sale in related:
            currencies[sale["currency"]].append(sale)
            past_area = area_cm2(sale["dimensions_raw"])
            past_medium = loader.PAINTING.fullmatch(sale["medium"])
            if (sale["hammer_price"] and target_area and past_area and target_medium and past_medium
                    and target_medium["paint"].casefold() == past_medium["paint"].casefold()
                    and Decimal("0.5") <= past_area / target_area <= 2):
                comparable[sale["currency"]].append({
                    key: sale[key] for key in ("title", "medium", "dimensions_raw", "sale_date",
                                               "currency", "hammer_price", "lot_url", "catalogue_url")
                } | {"price": sale["hammer_price"]})
        briefs.append({"lot_id": row["lot_id"], "artist": row["artist"], "category": "painting",
                       "available_sales_count": len(related),
                       "groups": {code: stats(currencies[code]) for code in sorted(currencies)},
                       "hammer_comparables": {
                           "price_basis": "hammer_price",
                           "status": "available" if comparable else "unavailable",
                           "groups": {code: stats(comparable[code]) for code in sorted(comparable)},
                           "sales": [sale for code in sorted(comparable) for sale in comparable[code]],
                       },
                       "sales": related})
    return {
        "schema_version": 2, "source_scope": source_scope,
        "price_basis": "price_realised", "includes_buyer_premium": True,
        "window_start_utc": loader.utc_text(start), "cutoff_utc": loader.utc_text(cutoff),
        "generated_at_utc": loader.utc_text(generated), "archive_months_requested": months,
        "session_start_utc": loader.utc_text(deadline) if deadline is not None else None,
        "catalogues_requested": catalogues, "source_errors": errors,
        "coverage_status": "partial" if errors else "scanned" if catalogues else "no_catalogues_found",
        "lots": briefs,
    }


def history_for_snapshot(snapshot, fetch, now=loader.utc_now):
    with FileLock(snapshot.absolute().parent / ".collections.lock", timeout=30):
        if not snapshot.is_dir() or loader.is_link(snapshot) or snapshot.name.endswith(".incomplete"):
            raise loader.PipelineError("Select a completed snapshot")
        lots = loader.read_csv(snapshot / "lots.csv", loader.LOT_FIELDS)
        original = loader.hash_files(snapshot)
    report = build_history(lots, fetch, now)
    with FileLock(snapshot.absolute().parent / ".collections.lock", timeout=30):
        if not snapshot.is_dir() or loader.is_link(snapshot) or loader.hash_files(snapshot) != original:
            raise loader.PipelineError("Snapshot changed while collecting artist history")
        report["purpose"] = "retrospective_backfill"
        report["session_start_utc"] = lots[0]["session_start_utc"] or None
        path = snapshot.with_name(snapshot.name + loader.BACKFILL_SUFFIX)
        loader.write_json(path, report)
    return path


def fetch_public(url):
    with requests.Session() as session:
        retry = Retry(total=1, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        return loader.fetch_resource(session, url)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    print("Scanning five years of Christie’s catalogues for artist history...", file=sys.stderr)
    try:
        path = history_for_snapshot(args.snapshot, fetch_public)
    except (OSError, Timeout, *SOURCE_ERRORS) as exc:
        print(f"Artist history stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Saved artist history: {path.resolve()}")
    report = json.loads(path.read_text(encoding="utf-8"))
    print(f"Historical coverage: {report['coverage_status']}; "
          f"catalogues checked: {report['catalogues_requested']}; "
          f"source errors: {len(report['source_errors'])}")
    return 0 if report["coverage_status"] == "scanned" else 1


if __name__ == "__main__":
    sys.exit(main())
