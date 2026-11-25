import io
import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from PIL import Image

import load_sothebys as s


class SothebysTests(unittest.TestCase):
    def test_snapshot_and_agent_inputs_share_the_christies_contract(self):
        catalog = s.BASE + "/en/buy/auction/2026/modern-art"
        archive = s.BASE + "/en/buy/auction/2025/modern-art"
        photo = "https://sothebys-md.brightspotcdn.com/image.png"
        start = "2026-12-03T13:30Z"
        now = lambda: s.common.utc_datetime("2026-11-25T10:00Z")
        session = {"sessionId": 0, "scheduledOpeningDate": start, "stateV2": {"state": "PreOpen"},
                   "lotRange": {"fromLotDisplayNumber": "1", "toLotDisplayNumber": "10"}}
        auction = {"auctionId": "upcoming", "type": "Live", "state": "Opened",
                   "title": "Modern Art", "currencyV2": "USD", "sessions": [session],
                   "slug": {"year": "2026", "name": "modern-art"}}
        closed = dict(auction, auctionId="past", state="Closed",
                      dates={"closed": "2025-12-03T15:30Z"},
                      slug={"year": "2025", "name": "modern-art"})

        def html(sale, rows, lot_id=None):
            cache = {"Auction:1": sale}
            cache.update({f"{'LotV2' if lot_id else 'LotCard'}:{i}": row for i, row in enumerate(rows)})
            props = {"auctionId": sale["auctionId"], "lotId": lot_id, "apolloCache": cache}
            data = json.dumps({"props": {"pageProps": props}})
            return (f'<script id="__NEXT_DATA__">{data}</script><p>100 - 200 USD</p>').encode()

        def lot(i, sale, bid):
            return {"lotId": str(UUID(int=i)), "title": "Painting", "creatorsDisplayTitle": "Artist",
                    "lotNumber": {"lotDisplayNumber": str(i)}, "auction": {"auctionId": sale["auctionId"]},
                    "session": session, "sessionId": 0, "slug": {"lotSlug": f"painting-{i}"},
                    "description": "<p>oil on canvas</p><p>20 x 30 cm</p>",
                    "bidState": bid, "withdrawnState": {"state": "NotAffected"},
                    "estimateV2": {"lowEstimate": {"amount": "100"}, "highEstimate": {"amount": "200"}},
                    "media({})": {"images": [{"renditions": [{"width": 800, "url": photo}]}]}}

        cards = [lot(i, auction, {"isClosed": False, "sold": {"isSold": False}}) for i in range(1, 11)]
        past = lot(100, closed, {"sold": {"__typename": "ResultVisible", "isSold": True,
                   "premiums": {"finalPriceV2": {"amount": "150", "currency": "USD"}},
                   "hammerPriceV2": {"amount": "120", "currency": "USD"}}})
        image = io.BytesIO()
        Image.new("RGB", (200, 100)).save(image, "PNG")
        pages = {catalog: html(auction, cards), archive: html(closed, [past]), photo: image.getvalue()}
        for row in cards:
            pages[s.card_url(catalog, row)] = html(
                auction, [dict(row, slug=row["slug"]["lotSlug"])], row["lotId"])
        pages[s.card_url(archive, past)] = html(closed, [past], past["lotId"])
        with tempfile.TemporaryDirectory() as directory:
            args = s.parse_args(["--catalog-url", catalog, "--history-catalog-url", archive,
                                 "--limit", "10", "--output-dir", directory])
            snapshot, count = s.collect(args, pages.__getitem__, now=now)
            self.assertEqual(count, 10)
            self.assertEqual(len(s.common.read_csv(snapshot / "lots.csv", s.common.LOT_FIELDS)), 10)
            manifest = json.loads((snapshot / s.common.MANIFEST).read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "sothebys")
            self.assertTrue(manifest["analysis_ready_at_utc"])
            used = s.common.archive_used_lots(snapshot, {cards[0]["lotId"]})
            blind = json.loads((used / "blind" / "lots.json").read_text(encoding="utf-8"))["lots"][0]
            self.assertEqual(blind["hammer_comparables"]["groups"]["USD"]["median"], "120")
            self.assertNotIn("lot_url", blind)
            self.assertTrue((used / "arbiter" / "estimates.json").is_file())

            root = Path(directory)
            args.skip_history = True
            old_catalogue, _ = s.collect(args, pages.__getitem__, now=now)
            modified_catalogue, _ = s.collect(args, pages.__getitem__, now=now)
            (modified_catalogue / "notes.txt").write_text("Research notes", encoding="utf-8")
            legacy = root / "older-attempt.incomplete"
            (legacy / "images").mkdir(parents=True)
            s.common.write_log(legacy / "errors.txt", [(catalog, "snapshot", "Catalogue failed")])
            s.common.write_manifest(legacy, s.common.utc_datetime(start))
            manifest_path = legacy / s.common.MANIFEST
            legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del legacy_manifest["source"]
            s.common.write_json(manifest_path, legacy_manifest)

            christies = root / "other-house"
            (christies / "images").mkdir(parents=True)
            s.common.write_manifest(christies, s.common.utc_datetime(start))
            later_start = "2026-12-10T13:30Z"
            later_now = lambda: s.common.utc_datetime("2026-12-05T10:00Z")
            refreshed = {url: payload.replace(start.encode(), later_start.encode()) if url != photo else payload
                         for url, payload in pages.items()}
            failed = {**refreshed, photo: b"invalid image"}
            with self.assertRaises(s.common.PipelineError):
                s.collect(args, failed.__getitem__, now=later_now)
            self.assertTrue(old_catalogue.exists())

            fresh, _ = s.collect(args, refreshed.__getitem__, now=later_now)
            self.assertTrue(fresh.exists())
            self.assertFalse(snapshot.exists())
            self.assertFalse(old_catalogue.exists())
            self.assertFalse(legacy.exists())
            self.assertTrue(used.exists())
            self.assertTrue(modified_catalogue.exists())
            self.assertTrue(christies.exists())

    def test_live_cutoff_differs_from_advance_bidding(self):
        cache = {"s": {"state": "PreOpen"}}
        auction = {"type": "Live", "state": "Opened", "sessions": [
            {"sessionId": 0, "scheduledOpeningDate": "2026-12-03T13:30Z",
             "stateV2": {"__ref": "s"}, "lotRange": {
                 "fromLotDisplayNumber": "301", "toLotDisplayNumber": "468"}}],
            "dates": {"acceptsBids": "2026-11-24T13:30Z"}}
        session, = s.upcoming_sessions(auction, cache, s.common.utc_datetime("2026-11-25T10:00Z"))
        self.assertEqual(session[0], s.common.utc_datetime("2026-12-03T13:30Z"))

    def test_hidden_results_are_not_unsold(self):
        self.assertIsNone(s.public_result({"sold": {"__typename": "ResultHidden"}}, "USD"))
        self.assertEqual(s.public_result(
            {"sold": {"__typename": "ResultVisible", "isSold": False}}, "USD"), (None, None))
