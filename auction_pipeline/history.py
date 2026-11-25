"""Shared historical price statistics and selection of comparables."""

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal

from . import common


LIFESPAN = re.compile(r"\s*\((?P<dates>(?:b(?:orn)?\.?\s*)?\d{4}(?:\s*[-–—]\s*\d{4})?)\)\s*$", re.I)
def five_years_before(cutoff):
    try:
        return cutoff.replace(year=cutoff.year - 5)
    except ValueError:
        return cutoff.replace(year=cutoff.year - 5, day=28)


def artist_identity(value):
    name = unicodedata.normalize("NFKC", common.plain_text(value)).casefold()
    suffix = LIFESPAN.search(name)
    dates = tuple(re.findall(r"\d{4}", suffix["dates"])) if suffix else ()
    return " ".join(name[:suffix.start() if suffix else len(name)].split()), dates


def area_cm2(dimensions):
    pattern = r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*cm\b"
    lines = dimensions.replace(r"\n", "\n").splitlines()
    measurements = [line for line in lines if not re.match(r"\s*(?:overall|frame[d]?)\b", line, re.I)]
    matches = [match for line in measurements for match in re.finditer(pattern, line, re.I)]
    if len(matches) != 1:
        return None
    height, width = map(Decimal, matches[0].groups())
    return height * width if height > 0 and width > 0 else None


def stats(sales):
    values = sorted(Decimal(sale["price"]) for sale in sales)

    def quantile(numerator):
        index, remainder = divmod((len(values) - 1) * numerator, 4)
        return values[index] if not remainder else (
            values[index] + (values[index + 1] - values[index]) * Decimal(remainder) / 4)

    summary = {"q25": quantile(1), "median": quantile(2), "q75": quantile(3),
               "mean": sum(values) / len(values)}
    return {"count": len(values), **{key: format(value.normalize(), "f") for key, value in summary.items()}}


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
        target_medium = common.PAINTING.fullmatch(row.get("medium_raw", ""))
        for sale in related:
            currencies[sale["currency"]].append(sale)
            past_area = area_cm2(sale["dimensions_raw"])
            past_medium = common.PAINTING.fullmatch(sale["medium"])
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
        "window_start_utc": common.utc_text(start), "cutoff_utc": common.utc_text(cutoff),
        "generated_at_utc": common.utc_text(generated), "archive_months_requested": months,
        "session_start_utc": common.utc_text(deadline) if deadline is not None else None,
        "catalogues_requested": catalogues, "source_errors": errors,
        "coverage_status": "partial" if errors else "scanned" if catalogues else "no_catalogues_found",
        "lots": briefs,
    }
