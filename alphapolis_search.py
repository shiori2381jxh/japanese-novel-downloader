"""AlphaPolis 小说搜索页的筛选 URL 与解析逻辑。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
import requests


ALPHAPOLIS_SEARCH_URL = "https://www.alphapolis.co.jp/novel/index"
SEARCH_LIMIT = 40
RETRY_DELAYS = (5, 10, 20, 40)
_WORK_PATH = re.compile(r"^/novel/\d+/\d+/?$")


@dataclass(frozen=True)
class SearchFilters:
    category_id: str
    complete_only: bool
    char_min: int | None
    char_max: int | None
    sort: str
    limit: int = SEARCH_LIMIT


@dataclass(frozen=True)
class SearchPage:
    novel_urls: list[str]
    next_url: str | None


def validate_filters(filters: SearchFilters) -> None:
    if not filters.category_id.isdigit():
        raise ValueError("题材无效")
    if filters.char_min is not None and filters.char_min < 0:
        raise ValueError("最小字数不能小于 0")
    if filters.char_max is not None and filters.char_max < 0:
        raise ValueError("最大字数不能小于 0")
    if (filters.char_min is not None and filters.char_max is not None
            and filters.char_min > filters.char_max):
        raise ValueError("最小字数不能大于最大字数")
    if filters.limit != SEARCH_LIMIT:
        raise ValueError("每页数量必须为 40")


def build_search_url(filters: SearchFilters, page: int = 1) -> str:
    validate_filters(filters)
    if page < 1:
        raise ValueError("页码必须大于 0")
    query = {
        "category_ids": filters.category_id,
        "is_inside": "1",
        "sort": filters.sort,
        "limit": str(filters.limit),
        "page": str(page),
    }
    if filters.complete_only:
        query["complete"] = "1"
    if filters.char_min is not None:
        query["char_min"] = str(filters.char_min)
    if filters.char_max is not None:
        query["char_max"] = str(filters.char_max)
    return f"{ALPHAPOLIS_SEARCH_URL}?{urlencode(query)}"


def parse_search_page(html: str, page_url: str) -> SearchPage:
    soup = BeautifulSoup(html, "html.parser")
    novel_urls: list[str] = []
    for anchor in soup.select("a[href]"):
        absolute = urljoin(page_url, anchor["href"]).rstrip("/")
        if _WORK_PATH.fullmatch(urlparse(absolute).path) and absolute not in novel_urls:
            novel_urls.append(absolute)
    next_link = soup.select_one('a[rel="next"][href]')
    if next_link:
        next_url = urljoin(page_url, next_link["href"])
    else:
        try:
            current_page = int(parse_qs(urlparse(page_url).query).get("page", ["1"])[0])
        except ValueError:
            current_page = 1
        next_url = None
        for anchor in soup.select("a[href]"):
            candidate = urljoin(page_url, anchor["href"])
            try:
                candidate_page = int(parse_qs(urlparse(candidate).query).get("page", ["0"])[0])
            except ValueError:
                continue
            if candidate_page == current_page + 1:
                next_url = candidate
                break
    return SearchPage(novel_urls, next_url)


class AlphaPolisSearchClient:
    """按网站分页顺序取得 AlphaPolis 搜索结果。"""

    def __init__(self, delay: float = 1.5, session=None, sleep=None):
        self.delay = max(0.0, float(delay))
        self.session = session or requests.Session()
        self.sleep = sleep or time.sleep
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        })

    def _get_search_page(self, url: str,
                         should_abort: Callable[[], bool]) -> SearchPage | None:
        for attempt in range(5):
            if should_abort():
                return None
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return parse_search_page(response.text, url)
            except requests.RequestException as exc:
                if attempt == 4:
                    raise RuntimeError(f"{url}：连续 5 次请求失败：{exc}") from exc
                self.sleep(RETRY_DELAYS[attempt])
        return None

    def iter_pages(self, filters: SearchFilters,
                   should_abort: Callable[[], bool]) -> Iterator[SearchPage]:
        next_url = build_search_url(filters)
        visited: set[str] = set()
        while next_url:
            if should_abort():
                return
            if next_url in visited:
                raise RuntimeError("AlphaPolis 搜索分页重复，已停止以避免循环")
            visited.add(next_url)
            page = self._get_search_page(next_url, should_abort)
            if page is None:
                return
            yield page
            next_url = page.next_url
            if next_url and self.delay and not should_abort():
                time.sleep(self.delay)
