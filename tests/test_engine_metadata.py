import contextlib
import io
import unittest
from unittest import mock

import novel_downloader as engine


class MetadataInspectionTests(unittest.TestCase):
    def test_aozora_work_info_parses_current_card_table_structure(self):
        html = (
            '<div align="center"><h1>図書カード：No.1567</h1></div>'
            '<a href="../../index_pages/person35.html">作家別作品リスト</a>'
            '<tr><td class="header">作品名：</td>'
            '<td><font size="+2">走れメロス</font></td></tr>'
            '<tr><td class="header">著者名：</td>'
            '<td><a href="../../index_pages/person35.html">太宰 治</a></td></tr>'
        )
        info = engine.aozora_get_work_info(html)
        self.assertEqual(info, {"title": "走れメロス", "author": "太宰 治"})

    def test_aozora_fetch_honors_utf8_meta_when_http_header_has_no_charset(self):
        html = (
            '<html><head><meta charset="utf-8"></head>'
            '<body><h1>図書カード：走れメロス</h1></body></html>'
        ).encode("utf-8")
        response = mock.MagicMock()
        response.headers.get_content_charset.return_value = None
        response.read.return_value = html
        response.__enter__.return_value = response
        with mock.patch.object(engine, "urlopen", return_value=response):
            decoded = engine.aozora_fetch_html("https://example.test/card.html")
        self.assertEqual(engine.aozora_get_work_info(decoded)["title"], "走れメロス")

    def test_aozora_metadata_inspection_does_not_download_zip(self):
        html = "<h1>図書カード：走れメロス</h1>"
        output = io.StringIO()
        url = "https://www.aozora.gr.jp/cards/000035/card1567.html"
        with (
            mock.patch.object(engine, "aozora_fetch_html", return_value=html),
            mock.patch.object(engine, "aozora_download_extract") as download_zip,
            contextlib.redirect_stdout(output),
        ):
            engine._main(["--inspect-metadata", url])
        self.assertIn("タイトル : 走れメロス", output.getvalue())
        download_zip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
