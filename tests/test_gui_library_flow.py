import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import novel_downloader_gui as gui
from library_awareness import Candidate, LibraryDataError
from alphapolis_search import SearchFilters, SearchPage


class SettingsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "settings.json"

    def test_schema_one_keeps_values_and_defaults_manual(self):
        old = {"schema": 1, "output_dir": "D:/Books", "delay": 2.0}
        self.path.write_text(json.dumps(old), encoding="utf-8")
        with mock.patch.object(gui, "settings_path", return_value=str(self.path)):
            loaded = gui.load_settings()
        self.assertEqual(loaded["output_dir"], "D:/Books")
        self.assertEqual(loaded["delay"], 2.0)
        self.assertEqual(loaded["library_mode"], "manual")
        self.assertEqual(loaded["schema"], 3)

    def test_schema_two_preserves_valid_auto_mode(self):
        current = {"schema": 2, "library_mode": "auto"}
        self.path.write_text(json.dumps(current), encoding="utf-8")
        with mock.patch.object(gui, "settings_path", return_value=str(self.path)):
            loaded = gui.load_settings()
        self.assertEqual(loaded["library_mode"], "auto")

    def test_invalid_mode_falls_back_to_manual(self):
        current = {"schema": 2, "library_mode": "unknown"}
        self.path.write_text(json.dumps(current), encoding="utf-8")
        with mock.patch.object(gui, "settings_path", return_value=str(self.path)):
            loaded = gui.load_settings()
        self.assertEqual(loaded["library_mode"], "manual")

    def test_default_library_path_points_to_shared_json(self):
        library_path = gui._default_library_path()
        self.assertEqual(library_path.name, "书库数据.json")
        self.assertIn("书库筛查数据", str(library_path))


class AlphaPolisSettingsTests(unittest.TestCase):
    def test_defaults_include_requested_romance_batch_filter(self):
        batch = gui.default_settings()["alphapolis_batch"]
        self.assertEqual(batch["category_id"], "110500")
        self.assertTrue(batch["complete_only"])
        self.assertEqual(batch["char_min"], 19000)
        self.assertEqual(batch["char_max"], 50000)
        self.assertEqual(batch["sort"], "favorite")
        self.assertEqual(batch["target_count"], 100)

    def test_target_count_rejects_zero(self):
        with self.assertRaisesRegex(ValueError, "下载数量必须是大于 0 的整数"):
            gui._batch_target_from_text("0")

    def test_filter_values_allow_empty_character_bounds(self):
        filters = gui._batch_filters_from_values("恋爱", True, "", "", "收藏数")
        self.assertEqual(filters.category_id, "110500")
        self.assertIsNone(filters.char_min)
        self.assertIsNone(filters.char_max)


class InspectionPolicyTests(unittest.TestCase):
    def test_inspection_args_are_read_only(self):
        args = gui._build_inspect_args("https://example.test/work", {"delay": 1.5})
        self.assertIn("--dry-run", args)
        self.assertNotIn("--output-dir", args)
        self.assertNotIn("--clean-text", args)
        self.assertNotIn("--split-text", args)

    def test_aozora_inspection_uses_metadata_only_engine_mode(self):
        url = "https://www.aozora.gr.jp/cards/000035/card1567.html"
        args = gui._build_inspect_args(url, {"delay": 1.5}, "aozora")
        self.assertEqual(args, ["--inspect-metadata", url])

    def test_auto_selects_only_new(self):
        rows = [
            Candidate("1", "1", "s", status="new", selected=True),
            Candidate("2", "2", "s", status="recorded", selected=True),
            Candidate("3", "3", "s", status="batch_duplicate", selected=True),
        ]
        self.assertEqual(
            [row.url for row in gui._selected_candidates(rows, "auto")], ["1"]
        )

    def test_manual_respects_user_selection_but_never_failed_rows(self):
        rows = [
            Candidate("1", "1", "s", status="recorded", selected=True),
            Candidate("2", "2", "s", status="new", selected=False),
            Candidate("3", "3", "s", status="inspect_failed", selected=True),
        ]
        self.assertEqual(
            [row.url for row in gui._selected_candidates(rows, "manual")], ["1"]
        )

    def test_identical_urls_are_preserved_for_batch_duplicate_classification(self):
        url = "https://example.test/work"
        self.assertEqual(
            gui.NovelDownloaderApp._extract_urls(f"{url}\n{url}"), [url, url]
        )


class InspectionWorkerTests(unittest.TestCase):
    def make_app(self):
        return SimpleNamespace(
            _abort_event=threading.Event(),
            _queue=queue.Queue(),
            _proc=None,
            _extract_urls=lambda _text: ["https://example.test/work"],
        )

    def test_worker_inspects_title_and_classifies_against_library(self):
        app = self.make_app()
        process = mock.Mock()
        process.communicate.return_value = ("  タイトル : 新作\n[dry-run]", None)
        process.returncode = 0
        detected = {
            "site": "narou",
            "display_name": "小説家になろう",
            "normalized_url": "https://example.test/normalized",
            "needs_playwright": False,
        }
        with (
            mock.patch.object(gui, "load_library",
                              return_value={"recorded_names": [], "categories": {}}),
            mock.patch.object(gui, "detect_site", return_value=detected),
            mock.patch.object(gui.subprocess, "Popen", return_value=process),
        ):
            gui.NovelDownloaderApp._inspection_worker(app, "input", {"delay": 1.5})

        messages = list(app._queue.queue)
        ready = next(message for message in messages if message[0] == "inspection_ready")
        candidate = ready[1][0]
        self.assertEqual(candidate.title, "新作")
        self.assertEqual(candidate.library_name, "新作.txt")
        self.assertEqual(candidate.status, "new")

    def test_worker_stops_batch_when_library_is_invalid(self):
        app = self.make_app()
        with mock.patch.object(gui, "load_library",
                               side_effect=LibraryDataError("broken")):
            gui.NovelDownloaderApp._inspection_worker(app, "input", {"delay": 1.5})
        self.assertEqual(app._queue.get_nowait(), ("library_error", "broken"))

    def test_worker_puts_inspection_failure_reason_in_raw_log(self):
        app = self.make_app()
        with (
            mock.patch.object(gui, "load_library",
                              return_value={"recorded_names": [], "categories": {}}),
            mock.patch.object(gui, "detect_site", return_value=None),
        ):
            gui.NovelDownloaderApp._inspection_worker(app, "input", {"delay": 1.5})
        raw_logs = [message[1] for message in app._queue.queue
                    if message[0] == "rawlog"]
        self.assertTrue(any("不支持该网站" in line for line in raw_logs))


class InspectionLaunchTests(unittest.TestCase):
    def test_shared_launcher_routes_raw_text_to_inspection_worker(self):
        worker_target = mock.Mock()
        app = SimpleNamespace(
            _proc=None,
            _epub_path="old",
            _raw_log=["old"],
            _abort_event=threading.Event(),
            _worker=None,
            _set_state_running=mock.Mock(),
            _inspection_worker=worker_target,
        )
        thread = mock.Mock()
        with mock.patch.object(gui.threading, "Thread", return_value=thread) as create:
            started = gui.NovelDownloaderApp._launch_inspection(
                app, "https://example.test/work", {"delay": 1.5}
            )
        self.assertTrue(started)
        create.assert_called_once_with(
            target=worker_target,
            args=("https://example.test/work", {"delay": 1.5}),
            daemon=True,
        )
        thread.start.assert_called_once_with()


class ManualDialogHelpersTests(unittest.TestCase):
    def test_status_labels_and_counts(self):
        rows = [
            Candidate("1", "1", "s", status="new"),
            Candidate("2", "2", "s", status="recorded"),
            Candidate("3", "3", "s", status="inspect_failed"),
        ]
        self.assertEqual(gui._status_text("recorded"), "已收录")
        self.assertEqual(
            gui._status_counts(rows),
            {
                "new": 1,
                "recorded": 1,
                "batch_duplicate": 0,
                "inspect_failed": 1,
            },
        )


class SummaryTests(unittest.TestCase):
    def test_summary_mentions_every_result(self):
        text = gui._format_summary({
            "downloaded": 2,
            "recorded_skipped": 3,
            "batch_skipped": 1,
            "inspect_failed": 1,
            "download_failed": 1,
            "record_failed": 1,
        })
        for fragment in (
            "成功下载 2",
            "已收录 3",
            "本批重复 1",
            "预检失败 1",
            "下载失败 1",
            "记录失败 1",
        ):
            self.assertIn(fragment, text)


class DownloadRecordingTests(unittest.TestCase):
    def make_app(self):
        return SimpleNamespace(
            _abort_event=threading.Event(),
            _queue=queue.Queue(),
            _proc=None,
            _build_cli_args=lambda _url, _settings: ["download"],
        )

    def settings(self):
        return {
            "library_mode": "manual",
            "output_dir": "unused",
            "cover_mode": "auto",
            "horizontal": False,
            "kobo": False,
            "toc_at_end": False,
            "font_path": "",
            "delay": 1.5,
            "encoding": "utf-8",
            "output_mode": "clean_txt",
        }

    def test_verified_success_records_canonical_library_name(self):
        with tempfile.TemporaryDirectory() as folder:
            txt = Path(folder) / "actual.txt"
            txt.write_text("body", encoding="utf-8")
            process = mock.Mock()
            process.stdout = [f"✅ テキスト出力完了: {txt}\n"]
            process.wait.return_value = 0
            candidate = Candidate(
                "u", "normalized", "site", "作品", "作品.txt", "new", True
            )
            app = self.make_app()
            with (
                mock.patch.object(gui.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(gui, "record_library_name") as record,
            ):
                gui.NovelDownloaderApp._download_worker(
                    app, [candidate], self.settings()
                )
            record.assert_called_once_with(gui._default_library_path(), "作品.txt")
            command = popen.call_args.args[0]
            self.assertIn("-o", command)
            self.assertEqual(command[command.index("-o") + 1], "作品")
            summary = list(app._queue.queue)[-1][1]
            self.assertEqual(summary["downloaded"], 1)
            self.assertEqual(summary["download_failed"], 0)

    def test_nonzero_exit_never_records_even_if_path_was_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            txt = Path(folder) / "actual.txt"
            txt.write_text("body", encoding="utf-8")
            process = mock.Mock()
            process.stdout = [f"✅ テキスト出力完了: {txt}\n"]
            process.wait.return_value = 1
            candidate = Candidate(
                "u", "normalized", "site", "作品", "作品.txt", "new", True
            )
            app = self.make_app()
            with (
                mock.patch.object(gui.subprocess, "Popen", return_value=process),
                mock.patch.object(gui, "record_library_name") as record,
            ):
                gui.NovelDownloaderApp._download_worker(
                    app, [candidate], self.settings()
                )
            record.assert_not_called()
            summary = list(app._queue.queue)[-1][1]
            self.assertEqual(summary["download_failed"], 1)

    def test_decorated_aozora_completion_line_extracts_only_txt_path(self):
        line = "✅ テキスト出力完了: C:/Books/source_name.txt  （shift_jis → UTF-8 変換済み）"
        match = gui._RE_TXT_DONE.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "C:/Books/source_name.txt")

    def test_stdout_failure_still_emits_terminal_download_summary(self):
        class BrokenStream:
            def __iter__(self):
                raise OSError("stream closed")

        process = mock.Mock()
        process.stdout = BrokenStream()
        candidate = Candidate(
            "u", "normalized", "site", "作品", "作品.txt", "new", True
        )
        app = self.make_app()
        with (
            mock.patch.object(gui.subprocess, "Popen", return_value=process),
            mock.patch.object(gui, "record_library_name") as record,
        ):
            gui.NovelDownloaderApp._download_worker(app, [candidate], self.settings())
        record.assert_not_called()
        terminal = list(app._queue.queue)[-1]
        self.assertEqual(terminal[0], "batch_finished")
        self.assertEqual(terminal[1]["download_failed"], 1)

    def test_cancellation_after_reported_path_does_not_record(self):
        with tempfile.TemporaryDirectory() as folder:
            txt = Path(folder) / "actual.txt"
            txt.write_text("body", encoding="utf-8")
            app = self.make_app()

            def output_lines():
                yield f"✅ テキスト出力完了: {txt}\n"
                app._abort_event.set()

            process = mock.Mock()
            process.stdout = output_lines()
            process.wait.return_value = 0
            candidate = Candidate(
                "u", "normalized", "site", "作品", "作品.txt", "new", True
            )
            with (
                mock.patch.object(gui.subprocess, "Popen", return_value=process),
                mock.patch.object(gui, "record_library_name") as record,
            ):
                gui.NovelDownloaderApp._download_worker(
                    app, [candidate], self.settings()
                )
            record.assert_not_called()
            self.assertEqual(list(app._queue.queue)[-1][0], "aborted")

    def test_split_txt_directory_is_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            split = Path(folder) / "作品"
            split.mkdir()
            (split / "作品_01.txt").write_text("part", encoding="utf-8")
            process = mock.Mock()
            process.stdout = [f"✅ 分割テキスト出力完了: {split}\n"]
            process.wait.return_value = 0
            candidate = Candidate(
                "u", "normalized", "site", "作品", "作品.txt", "new", True
            )
            app = self.make_app()
            with (
                mock.patch.object(gui.subprocess, "Popen", return_value=process),
                mock.patch.object(gui, "record_library_name") as record,
            ):
                gui.NovelDownloaderApp._download_worker(
                    app, [candidate], self.settings()
                )
            record.assert_called_once()

    def test_record_failure_keeps_download_and_reports_record_error(self):
        with tempfile.TemporaryDirectory() as folder:
            txt = Path(folder) / "actual.txt"
            txt.write_text("body", encoding="utf-8")
            process = mock.Mock()
            process.stdout = [f"✅ テキスト出力完了: {txt}\n"]
            process.wait.return_value = 0
            candidate = Candidate(
                "u", "normalized", "site", "作品", "作品.txt", "new", True
            )
            app = self.make_app()
            with (
                mock.patch.object(gui.subprocess, "Popen", return_value=process),
                mock.patch.object(gui, "record_library_name",
                                  side_effect=OSError("locked")),
            ):
                gui.NovelDownloaderApp._download_worker(
                    app, [candidate], self.settings()
                )
            summary = list(app._queue.queue)[-1][1]
            self.assertEqual(summary["downloaded"], 1)
            self.assertEqual(summary["record_failed"], 1)


class AlphaPolisBatchWorkerTests(unittest.TestCase):
    def test_verified_download_counts_and_records_before_reaching_target(self):
        app = SimpleNamespace(
            _abort_event=threading.Event(), _queue=queue.Queue(), _proc=None,
            _inspect_url_candidate=lambda _url, _settings: Candidate(
                "u", "u", "AlphaPolis", "新书", "新书.txt"),
            _download_candidate=lambda _candidate, _settings: (True, "C:/Books/新书.txt"),
        )
        filters = SearchFilters("110500", True, 19000, 50000, "favorite")
        with (
            mock.patch.object(gui, "AlphaPolisSearchClient") as client,
            mock.patch.object(gui, "load_library", return_value={"recorded_names": [], "categories": {}}),
            mock.patch.object(gui, "record_library_name") as record,
        ):
            client.return_value.iter_pages.return_value = iter([
                SearchPage(["https://www.alphapolis.co.jp/novel/1/2"], None)
            ])
            gui.NovelDownloaderApp._alphapolis_batch_worker(app, filters, 1, {"delay": 0})

        summary = list(app._queue.queue)[-1][1]
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["stop_reason"], "target_met")
        record.assert_called_once_with(gui._default_library_path(), "新书.txt")

    def test_recorded_result_is_not_counted_when_results_end(self):
        app = SimpleNamespace(
            _abort_event=threading.Event(), _queue=queue.Queue(), _proc=None,
            _inspect_url_candidate=lambda _url, _settings: Candidate(
                "u", "u", "AlphaPolis", "旧书", "旧书.txt"),
            _download_candidate=mock.Mock(),
        )
        filters = SearchFilters("110500", True, 19000, 50000, "favorite")
        with (
            mock.patch.object(gui, "AlphaPolisSearchClient") as client,
            mock.patch.object(gui, "load_library", return_value={"recorded_names": ["旧书.txt"], "categories": {}}),
        ):
            client.return_value.iter_pages.return_value = iter([
                SearchPage(["https://www.alphapolis.co.jp/novel/1/2"], None)
            ])
            gui.NovelDownloaderApp._alphapolis_batch_worker(app, filters, 100, {"delay": 0})

        summary = list(app._queue.queue)[-1][1]
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(summary["recorded_skipped"], 1)
        self.assertEqual(summary["stop_reason"], "results_exhausted")


if __name__ == "__main__":
    unittest.main()
