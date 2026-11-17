import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import load_christies as loader


OVERVIEW = 'https://www.christies.com/en/auction/auction-24268-nyr/overview'
CATALOG = OVERVIEW.replace('overview', 'browse-lots')
START = '2026-11-20T15:30:00Z'
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def manual_args(extra=()):
    return loader.parse_args(['--catalog-url', CATALOG, '--lot-from', '701',
                              '--lot-to', '864', *extra])


def component(name, value):
    return f'<script>window.chrComponents.{name} = {json.dumps(value)};</script>'


def pages():
    sale = dict(sale_id='31037', sale_number='24268', sale_room_code='NYR', title='Modern Art Day Sale',
                event_type='Sale', is_in_progress=False, is_auction_over=False)
    status = dict(is_in_progress=False, is_auction_over=False, sessions=[
        dict(lot_txt='Lots 701-864', date_time_universal=START)])
    auction = '<script>window.chrComponents.auction = {data: ' + json.dumps(
        dict(sale, auction_status=status)) + ', labels: {}};</script>'
    responses = {OVERVIEW: auction}
    catalog_rows = []
    for number in range(701, 713):
        url = f'https://www.christies.com/en/lot/lot-{number}'
        preview = dict(object_id=str(number), lot_id_txt=str(number), url=url,
                       description_txt='Artist<br>Title<br>oil on canvas',
                       price_realised='')
        catalog_rows.append(preview)
        lot = dict(preview, title_primary_txt='Artist', title_secondary_txt='Title',
                   estimate_txt='USD 1,000 – USD 2,000', estimate_low=1000,
                   estimate_high=2000, price_realised=1 if number == 701 else 0)
        header = component('lotHeader_12', dict(
            data=dict(lots=[lot], sale=sale), labels={'currency_txt': 'USD'}))
        photo = f'https://www.christies.com/img/{number}.jpg'
        responses[url] = header + (
            '<chr-accordion-item><div slot="header">Details</div>'
            '<div slot="content">Artist<br>Title<br>oil on canvas'
            '<br>canvas: 20 x 30 cm.<br>overall: 21 x 31 cm.'
            '<br>Painted in 1999.</div></chr-accordion-item>'
            f'<div region="lot-header-image"><img data-srcset="{photo} 600w"></div>')
    responses[CATALOG] = auction + component('lots', dict(data=dict(lots=catalog_rows)))
    return responses


class LoaderTests(unittest.TestCase):
    def test_end_to_end_keeps_valid_lots_and_logs_price_and_bad_image(self):
        image = io.BytesIO()
        Image.new('RGB', (200, 150)).save(image, 'PNG')
        responses = pages()

        def fetch(url):
            if url in responses:
                return responses[url].encode()
            return b'broken' if url.endswith('/702.jpg') else image.getvalue()

        with tempfile.TemporaryDirectory() as folder:
            path, count = loader.collect(manual_args([
                '--output-dir', folder, '--limit', '10']), fetch, lambda: NOW)
            self.assertEqual(count, 10)
            with (path / 'lots.csv').open(encoding='utf-8-sig', newline='') as file:
                lots = list(csv.DictReader(file))
            with (path / 'estimates.csv').open(encoding='utf-8-sig', newline='') as file:
                estimates = list(csv.DictReader(file))
            self.assertEqual([row['lot_id'] for row in lots], [row['lot_id'] for row in estimates])
            self.assertEqual(lots[0]['lot_id'], '703')
            self.assertEqual(lots[0]['image_path'], 'images/703.png')
            self.assertEqual(lots[0]['creation_date_raw'], 'Painted in 1999.')
            self.assertEqual(lots[0]['dimensions_raw'], r'canvas: 20 x 30 cm.\noverall: 21 x 31 cm.')
            self.assertEqual(len((path / 'lots.csv').read_text(encoding='utf-8-sig').splitlines()), 11)
            self.assertIn('Modern Art Day Sale', path.name)
            self.assertEqual(estimates[0]['estimate_currency'], 'USD')
            self.assertNotIn('price_realised', lots[0])
            self.assertIn('lot-701', (path / 'errors.txt').read_text(encoding='utf-8'))
            self.assertIn('lot-702', (path / 'errors.txt').read_text(encoding='utf-8'))
            with Image.open(path / lots[0]['image_path']) as saved:
                saved.load()

    def test_new_snapshot_replaces_expired_unmodified_data_and_retains_used_lots(self):
        responses = pages()
        image = io.BytesIO()
        Image.new('RGB', (200, 150)).save(image, 'PNG')

        def fetch(url):
            return responses[url].encode() if url in responses else image.getvalue()

        march = datetime(2027, 3, 1, tzinfo=timezone.utc)
        later = '2027-03-04T15:30:00Z'
        with tempfile.TemporaryDirectory() as folder:
            old_args = manual_args(['--output-dir', folder, '--limit', '10'])
            old, _ = loader.collect(old_args, fetch, lambda: NOW)
            edited, _ = loader.collect(old_args, fetch, lambda: NOW)
            with (edited / 'errors.txt').open('a', encoding='utf-8') as file:
                file.write('analysis notes\n')
            saved = loader.archive_used_lots(old, {'702', '703'})
            old_failures = dict(responses)
            responses = {url: html.replace(START, later) for url, html in responses.items()}
            fresh_args = manual_args(['--output-dir', folder, '--limit', '10',
                                            '--session-start', later])
            with self.assertRaises(loader.PipelineError):
                loader.collect(fresh_args, lambda url: old_failures[url].encode(), lambda: march)
            self.assertTrue(old.exists())
            fresh, _ = loader.collect(fresh_args, fetch, lambda: march)
            self.assertTrue(fresh.exists())
            self.assertFalse(old.exists())
            self.assertTrue(edited.exists())
            self.assertEqual({p.stem for p in (saved / 'images').iterdir()}, {'702', '703'})
            with (saved / 'lots.csv').open(encoding='utf-8-sig', newline='') as file:
                self.assertEqual([row['lot_id'] for row in csv.DictReader(file)], ['702', '703'])

    def test_deadline_rejects_before_any_http_request(self):
        with self.assertRaisesRegex(loader.PipelineError, 'deadline'):
            loader.collect(manual_args(['--session-start', START]), lambda _: self.fail('HTTP requested'),
                           lambda: loader.utc_datetime(START))

    def test_conflicting_schedule_blocks_collection(self):
        with self.assertRaisesRegex(loader.PipelineError, 'schedule'):
            loader.collect(manual_args(['--session-start', '2026-11-20T14:30Z']),
                           lambda url: pages()[url].encode(), lambda: NOW)

    def test_live_status_on_a_card_stops_the_snapshot(self):
        responses = pages()
        url = 'https://www.christies.com/en/lot/lot-701'
        responses[url] = responses[url].replace('"is_in_progress": false',
                                                '"is_in_progress": true')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(loader.PipelineError, 'status'):
                loader.collect(manual_args(['--output-dir', folder]),
                               lambda url: responses[url].encode(), lambda: NOW)
            attempts = list(Path(folder).iterdir())
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].suffix, '.incomplete')
            self.assertIn('status', (attempts[0] / 'errors.txt').read_text(encoding='utf-8'))

    def test_automatic_collection_after_pilot_uses_next_month_and_official_session_time(self):
        later = '2026-12-01T17:00:00Z'
        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        calendar = 'https://www.christies.com/en/calendar'
        new_auction = 'https://www.christies.com/en/auction/auction-31216'
        responses = pages()
        responses[calendar] = (
            '<h3><a data-qa="auction-card-primary-link" href="/en/auction/auction-24268-nyr">'
            'Post-War and Contemporary Art Day Sale</a></h3>')
        responses[f'{calendar}/2026/12'] = (
            '<h3><a data-qa="auction-card-primary-link" href="/en/auction/auction-31216">'
            'Old Masters to Modern Day Sale: Paintings</a></h3>')
        for suffix, old in [('overview', OVERVIEW), ('browse-lots', CATALOG)]:
            responses[f'{new_auction}/{suffix}'] = responses[old].replace(START, later)
        responses[CATALOG] = 'Expired catalog should never be opened'
        image = io.BytesIO()
        Image.new('RGB', (200, 150)).save(image, 'PNG')

        def fetch(url):
            if url == CATALOG:
                self.fail('Expired catalog was opened')
            return responses[url].encode() if url in responses else image.getvalue()

        with tempfile.TemporaryDirectory() as folder:
            path, count = loader.collect(loader.parse_args(['--output-dir', folder]), fetch, lambda: now)
            self.assertEqual(count, 11)
            rows = loader.read_csv(path / 'lots.csv', loader.LOT_FIELDS)
            self.assertEqual({r['session_start_utc'] for r in rows}, {later})
            self.assertEqual({r['cutoff_source_url'] for r in rows}, {f'{new_auction}/overview'})

    def test_painting_filter_uses_whole_lines(self):
        self.assertEqual(loader.painting_medium('signed in oil\nOil on linen'), 'Oil on linen')
        self.assertEqual(loader.painting_medium('oil on canvas, in artist’s frame'), 'oil on canvas, in artist’s frame')
        for medium in ('oil on canvas print', 'mixed media on canvas',
                       'oil on paper mounted on canvas', 'oil on printed canvas'):
            self.assertEqual(loader.painting_medium(medium), '')


if __name__ == '__main__':
    unittest.main()
