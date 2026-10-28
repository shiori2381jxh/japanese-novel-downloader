import unittest
from urllib.parse import parse_qs, urlparse

import requests

from alphapolis_search import (
    AlphaPolisSearchClient,
    SearchFilters,
    build_search_url,
    parse_search_page,
    validate_filters,
)


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.headers = {}

    def get(self, _url, timeout):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SearchUrlTests(unittest.TestCase):
    def test_build_url_keeps_selected_filters_on_later_page(self):
        filters = SearchFilters(
            category_id="110500",
            complete_only=True,
            char_min=19000,
            char_max=50000,
            sort="favorite",
        )

        query = parse_qs(urlparse(build_search_url(filters, page=3)).query)

        self.assertEqual(query, {
            "category_ids": ["110500"], "is_inside": ["1"],
            "complete": ["1"], "char_min": ["19000"],
            "char_max": ["50000"], "sort": ["favorite"],
            "limit": ["40"], "page": ["3"],
        })

    def test_rejects_character_range_with_minimum_above_maximum(self):
        filters = SearchFilters("110500", True, 50000, 19000, "favorite")

        with self.assertRaisesRegex(ValueError, "最小字数不能大于最大字数"):
            validate_filters(filters)


class SearchPageTests(unittest.TestCase):
    def test_parser_returns_unique_work_urls_and_next_page(self):
        html = """
        <a href="/novel/111/222">first</a>
        <a href="https://www.alphapolis.co.jp/novel/111/222/">same work</a>
        <a href="/novel/333/444">second</a>
        <a href="/novel/index?page=2" rel="next">next</a>
        <a href="/novel/index?category_ids=110500">category</a>
        """

        page = parse_search_page(html, "https://www.alphapolis.co.jp/novel/index?page=1")

        self.assertEqual(page.novel_urls, [
            "https://www.alphapolis.co.jp/novel/111/222",
            "https://www.alphapolis.co.jp/novel/333/444",
        ])
        self.assertEqual(
            page.next_url,
            "https://www.alphapolis.co.jp/novel/index?page=2",
        )

    def test_parser_uses_next_numbered_page_when_site_omits_rel_next(self):
        html = """
        <a href="/novel/111/222">first</a>
        <a href="/novel/index?category_ids=110500&amp;page=2">2</a>
        <a href="/novel/index?category_ids=110500&amp;page=3">3</a>
        """

        page = parse_search_page(
            html,
            "https://www.alphapolis.co.jp/novel/index?category_ids=110500&page=1",
        )

        self.assertEqual(
            page.next_url,
            "https://www.alphapolis.co.jp/novel/index?category_ids=110500&page=2",
        )


class SearchRetryTests(unittest.TestCase):
    def setUp(self):
        self.filters = SearchFilters("110500", True, 19000, 50000, "favorite")

    def test_retries_the_same_page_and_yields_it_after_transient_errors(self):
        session = FakeSession([
            requests.RequestException("timeout"),
            requests.RequestException("too many requests"),
            FakeResponse('<a href="/novel/1/2">work</a>'),
        ])
        waits = []
        client = AlphaPolisSearchClient(0, session=session, sleep=waits.append)

        page = next(client.iter_pages(self.filters, lambda: False))

        self.assertEqual(page.novel_urls, ["https://www.alphapolis.co.jp/novel/1/2"])
        self.assertEqual(session.calls, 3)
        self.assertEqual(waits, [5, 10])

    def test_stops_after_five_failed_requests_without_a_fifth_wait(self):
        session = FakeSession([requests.RequestException("offline")] * 5)
        waits = []
        client = AlphaPolisSearchClient(0, session=session, sleep=waits.append)

        with self.assertRaisesRegex(RuntimeError, "连续 5 次请求失败"):
            next(client.iter_pages(self.filters, lambda: False))

        self.assertEqual(session.calls, 5)
        self.assertEqual(waits, [5, 10, 20, 40])

    def test_cancellation_during_wait_prevents_the_next_retry(self):
        cancelled = [False]

        def sleep(_seconds):
            cancelled[0] = True

        session = FakeSession([requests.RequestException("timeout")])
        client = AlphaPolisSearchClient(0, session=session, sleep=sleep)

        self.assertEqual(list(client.iter_pages(self.filters, lambda: cancelled[0])), [])
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
