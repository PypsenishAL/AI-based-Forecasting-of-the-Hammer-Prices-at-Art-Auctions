import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import artist_history as history


class HistoryTests(unittest.TestCase):
    def test_oil_on_vase_is_excluded(self):
        self.assertEqual(history.painting_technique("Artist<br>Title<br>oil on ceramic vase"), "")

    def test_build_groups_verified_paintings_by_artist_and_currency(self):
        target = [
            dict(lot_id="100", artist="HELEN FRANKENTHALER (1928-2011)",
                 collected_at_utc="2026-11-17T07:00:00Z", dimensions_raw="10 x 20 cm.",
                 medium_raw="oil on canvas"),
            dict(lot_id="101", artist="Helen Frankenthaler (1928–2011)",
                 collected_at_utc="2026-11-17T07:01:00Z"),
            dict(lot_id="102", artist="Unrepresented Artist",
                 collected_at_utc="2026-11-17T07:02:00Z"),
        ]
        catalog = "https://www.christies.com/en/auction/auction-29245/browse-lots"
        archive = ('<h1>Auction results</h1><h3><a data-qa="auction-card-primary-link" '
                   'href="/en/auction/auction-29245">Post-War and Contemporary Art Day Sale</a></h3>')
        auction = {"data": {"event_type": "Sale", "auction_status": {
            "is_auction_over": True, "is_in_progress": False, "start_date": "2022-05-13T13:30Z"}}}

        def lot(identity, price, currency="USD", medium="oil on canvas", **extra):
            return dict(object_id=str(identity), title_primary_txt="HELEN FRANKENTHALER (1928-2011)",
                        description_txt=f"Artist<br>Title<br>{medium}<br>10 x 20 cm.<br>Painted in 1982.",
                        price_realised=str(price), price_realised_txt=f"{currency} {price}",
                        start_date="2022-05-13T00:00Z", **extra)

        lots = [lot(1, 100, hammer_price_txt="USD 80", hammer_price=80),
                lot(2, 200), lot(3, 300), lot(4, 400), lot(5, 500, "GBP"),
                lot(6, 900, medium="screenprint on paper"), lot(7, 0),
                lot(8, 999, lot_withdrawn=True), lot(100, 1234),
                dict(lot(9, 999), title_primary_txt="AFTER HELEN FRANKENTHALER"),
                dict(lot(10, 999), start_date="2026-11-17T00:00Z")]
        data = {"data": {"lots": lots, "total_hits_filtered": len(lots)}, "labels": {"currency_txt": "USD"}}
        html = "window.chrComponents.auction = " + json.dumps(auction) + ";"
        html += "window.chrComponents.lots = " + json.dumps(data) + ";"

        def fetch(url):
            if url == catalog:
                return html.encode()
            if url == "https://www.christies.com/en/results/2022/05":
                return archive.encode()
            return b"<h1>Auction results</h1>"

        result = history.build_history(target, fetch, now=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
        rows = result["lots"]
        self.assertEqual([row["available_sales_count"] for row in rows], [5, 5, 0])
        self.assertEqual(rows[0]["groups"]["USD"]["q25"], "175")
        self.assertEqual(rows[0]["groups"]["USD"]["median"], "250")
        self.assertEqual(rows[0]["groups"]["USD"]["mean"], "250")
        self.assertEqual(rows[0]["groups"]["USD"]["q75"], "325")
        self.assertEqual(rows[0]["groups"]["GBP"]["count"], 1)
        self.assertEqual(rows[0]["hammer_comparables"]["groups"]["USD"]["median"], "80")
        self.assertEqual(rows[0]["hammer_comparables"]["sales"][0]["dimensions_raw"], "10 x 20 cm.")
        self.assertEqual(result["window_start_utc"], "2021-11-17T07:00:00Z")
        self.assertEqual(result["cutoff_utc"], "2026-11-17T07:00:00Z")
        self.assertEqual(result["price_basis"], "price_realised")

    def test_paginated_sources_keep_first_page_and_record_a_failed_page(self):
        catalog = "https://www.christies.com/en/auction/auction-29245/browse-lots"
        auction = {"data": {"event_type": "Sale", "auction_status": {
            "is_auction_over": True, "is_in_progress": False, "start_date": "2022-05-13T13:30Z"}}}
        first = {"object_id": "99", "title_primary_txt": "Helen Frankenthaler",
                 "description_txt": "Helen Frankenthaler<br>Artwork<br>acrylic and marker on canvas",
                 "price_realised": "1000", "price_realised_txt": "USD 1,000"}
        endpoint = {"path": "api/discoverywebsite/auctionpages/lotsearch",
                    "parameters": {"saleid": "29245", "salenumber": "20099", "saleroomcode": "NYR",
                                   "pagesize": "1", "page": "1"}}
        block = {"data": {"lots": [first], "total_hits_filtered": 3, "lot_search_api_endpoint": endpoint}}
        html = "window.chrComponents.auction = " + json.dumps(auction) + ";"
        html += "window.chrComponents.lots = " + json.dumps(block) + ";"

        def fetch(url):
            if url == catalog:
                return html.encode()
            if "page=2" in url and "sortby=lotnumber" in url and "searchphrase" not in url:
                return json.dumps({"total_hits_filtered": 3,
                                   "lots": [dict(first, object_id="100")]}).encode()
            return b"Unavailable"

        sales, errors = history.catalogue_sales(
            catalog, fetch, {"helen frankenthaler"}, set(),
            history.loader.utc_datetime("2021-11-17T07:00Z"),
            history.loader.utc_datetime("2026-11-17T07:00Z"))
        self.assertEqual([row["historical_lot_id"] for _, _, row in sales], ["99", "100"])
        self.assertEqual(len(errors), 2)
        self.assertIn("page=3", errors[0]["url"])

    def test_backfill_does_not_modify_snapshot_or_unlock_mark_used(self):
        loader = history.loader
        timestamp = "2026-11-17T07:00:00Z"
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot"
            images = snapshot / "images"
            images.mkdir(parents=True)
            lots, estimates = [], []
            for lot_id in ("101", "102"):
                image_path = f"images/{lot_id}.jpg"
                (snapshot / image_path).write_bytes(b"image")
                lots.append(dict(lot_id=lot_id, artist="Example Artist",
                                 collected_at_utc=timestamp, image_path=image_path))
                estimates.append(dict(lot_id=lot_id))
            loader.write_csv(snapshot / "lots.csv", loader.LOT_FIELDS, lots)
            loader.write_csv(snapshot / "estimates.csv", loader.ESTIMATE_FIELDS, estimates)
            loader.write_manifest(snapshot, loader.utc_datetime("2026-11-20T15:30Z"))
            path = history.history_for_snapshot(snapshot, lambda _: b"<h1>Auction results</h1>")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["coverage_status"],
                             "no_catalogues_found")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["purpose"], "retrospective_backfill")
            self.assertFalse((snapshot / loader.HISTORY_FILE).exists())
            manifest = json.loads((snapshot / loader.MANIFEST).read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"], loader.hash_files(snapshot))
            with self.assertRaises(loader.HistoryReadinessError):
                loader.archive_used_lots(snapshot, {"101"})

    def test_online_catalogue_uses_exact_lot_close_and_cumulative_last_page(self):
        alias = "https://www.christies.com/en/auction/auction-30749/browse-lots"
        archive = ('<h1>Auction results</h1><h3><a data-qa="auction-card-primary-link" '
                   'href="/nav/onlineonly/sso?SaleID=30749&amp;SaleNumber=23711">'
                   'First Open | Post-War &amp; Contemporary Art</a></h3>')
        self.assertEqual(list(history.archive_catalogues(archive)), [alias])
        auction = {"data": {"event_type": "OnlineSale", "auction_status": {
            "is_auction_over": True, "is_in_progress": False, "start_date": "2026-11-17T00:00Z"}}}
        lot = {"object_id": "263512", "url": "/s/first-open/helen-frankenthaler/263512?ldp_breadcrumb=back",
               "title_primary_txt": "Helen Frankenthaler", "end_date": "2026-11-17T06:00Z",
               "description_txt": "Helen Frankenthaler<br>Untitled<br>acrylic and pastel on paper",
               "price_realised": "75600", "price_realised_txt": "USD 75,600"}
        future = dict(lot, object_id="263513", url="/s/first-open/helen-frankenthaler/263513",
                      end_date="2026-11-17T08:00Z")
        endpoint = {"parameters": {"saleid": "3810"}}

        def packed(rows):
            block = {"data": {"lots": rows, "total_hits_filtered": 2, "total_pages": 2,
                              "lot_search_api_endpoint": endpoint}}
            return ("window.chrComponents = " + json.dumps({"auction": auction, "lots": block})).encode()

        responses = {alias: packed([lot]),
                     "https://onlineonly.christies.com/s/first-open/lots/3810?page=2": packed([lot, future])}
        sales, errors = history.catalogue_sales(
            alias, responses.__getitem__, {"helen frankenthaler"}, set(),
            history.loader.utc_datetime("2021-11-17T07:00Z"),
            history.loader.utc_datetime("2026-11-17T07:00Z"))
        self.assertEqual(errors, [])
        sale, = [row for _, _, row in sales]
        self.assertEqual(sale["sale_format"], "online")
        self.assertEqual(sale["lot_url"], "https://onlineonly.christies.com/s/first-open/helen-frankenthaler/263512")

    def test_calendar_window_handles_leap_day(self):
        cutoff = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)
        self.assertEqual(history.five_years_before(cutoff), datetime(2019, 2, 28, 12, tzinfo=timezone.utc))
