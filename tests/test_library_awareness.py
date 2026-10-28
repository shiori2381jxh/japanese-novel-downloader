import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import library_awareness as awareness
from library_awareness import (
    Candidate,
    LibraryDataError,
    classify_candidates,
    load_library,
    parse_dry_run_title,
    record_library_name,
    safe_library_name,
    verify_txt_output,
)


class FilenameAndParsingTests(unittest.TestCase):
    def test_safe_library_name_matches_engine_rule(self):
        self.assertEqual(safe_library_name('  A/B:*?"<>|  '), "A_B_______.txt")
        self.assertEqual(safe_library_name("あ" * 70), "あ" * 60 + ".txt")

    def test_safe_library_name_uses_fallback_for_empty_title(self):
        self.assertEqual(safe_library_name("  ", "fallback"), "fallback.txt")

    def test_parse_dry_run_title_accepts_current_engine_label(self):
        output = "      タイトル    : 作品名\n      作者: 作者名\n[dry-run]"
        self.assertEqual(parse_dry_run_title(output), "作品名")

    def test_parse_dry_run_title_returns_empty_when_missing(self):
        self.assertEqual(parse_dry_run_title("network error"), "")


class LibraryPersistenceTests(unittest.TestCase):
    def test_missing_library_loads_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            data = load_library(Path(folder) / "library.json")
            self.assertEqual(data, {"recorded_names": [], "categories": {}})

    def test_malformed_existing_library_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(LibraryDataError):
                load_library(path)

    def test_invalid_existing_library_shape_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.json"
            path.write_text('{"recorded_names": "not-a-list", "categories": {}}',
                            encoding="utf-8")
            with self.assertRaises(LibraryDataError):
                load_library(path)

    def test_record_reloads_unions_and_preserves_categories(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.json"
            path.write_text(json.dumps({
                "recorded_names": ["旧书.txt"],
                "categories": {"异世界": ["旧书.txt"]},
            }, ensure_ascii=False), encoding="utf-8")
            record_library_name(path, "新书.txt")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["recorded_names"], ["新书.txt", "旧书.txt"])
            self.assertEqual(saved["categories"], {"异世界": ["旧书.txt"]})

    def test_record_holds_exclusive_lock_for_read_merge_write_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.json"
            entered = []

            @contextmanager
            def fake_lock(_path):
                entered.append(True)
                yield

            with mock.patch.object(awareness, "_exclusive_library_lock", fake_lock):
                record_library_name(path, "新书.txt")
            self.assertEqual(entered, [True])

    def test_replace_failure_removes_temporary_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "library.json"
            with mock.patch.object(awareness.os, "replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    record_library_name(path, "新书.txt")
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])


class ClassificationAndOutputTests(unittest.TestCase):
    def test_classifies_recorded_new_failed_and_batch_duplicate(self):
        rows = [
            Candidate("u1", "u1", "site", "旧书", "旧书.txt"),
            Candidate("u2", "u2", "site", "新书", "新书.txt"),
            Candidate("u3", "u3", "site", "新书", "新书.txt"),
            Candidate("u4", "u4", "site", error="timeout"),
        ]
        result = classify_candidates(rows, {"旧书.txt"})
        self.assertEqual([row.status for row in result],
                         ["recorded", "new", "batch_duplicate", "inspect_failed"])
        self.assertEqual([row.selected for row in result], [False, True, False, False])

    def test_recorded_status_takes_precedence_over_batch_duplicate(self):
        rows = [
            Candidate("u1", "u1", "site", "旧书", "旧书.txt"),
            Candidate("u2", "u2", "site", "旧书", "旧书.txt"),
        ]
        result = classify_candidates(rows, {"旧书.txt"})
        self.assertEqual([row.status for row in result], ["recorded", "recorded"])

    def test_failed_candidate_does_not_make_later_success_a_batch_duplicate(self):
        rows = [
            Candidate("u1", "u1", "site", "新书", "新书.txt", error="timeout"),
            Candidate("u2", "u2", "site", "新书", "新书.txt"),
        ]
        result = classify_candidates(rows, set())
        self.assertEqual([row.status for row in result], ["inspect_failed", "new"])

    def test_verifies_file_and_split_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            txt = root / "book.txt"
            txt.write_text("text", encoding="utf-8")
            split = root / "book"
            split.mkdir()
            (split / "book_01.txt").write_text("part", encoding="utf-8")
            self.assertTrue(verify_txt_output(str(txt)))
            self.assertTrue(verify_txt_output(str(split)))
            self.assertFalse(verify_txt_output(str(root / "missing.txt")))

    def test_rejects_non_txt_file_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            epub = root / "book.epub"
            epub.write_text("epub", encoding="utf-8")
            empty = root / "empty"
            empty.mkdir()
            self.assertFalse(verify_txt_output(str(epub)))
            self.assertFalse(verify_txt_output(str(empty)))


if __name__ == "__main__":
    unittest.main()
