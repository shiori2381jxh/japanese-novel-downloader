#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
novel_downloader_gui.py — 小説ePubダウンローダー（GUI 皮）

CLI ツール novel_downloader.py を、コマンドラインを使わない一般ユーザー向けに
包む Windows 向け GUI フロントエンド。設計は gui_v1_design.md を参照。

方針（gui_v1_design.md より）:
  ① エンジンはサブプロセスで起動（本体無改修）
  ② CustomTkinter による単一ウィンドウ
  ③ URL欄＋大ボタンの徹底ミニマル。全オプションは「詳細設定」の奥
  ＋ 起動時クリップボード自動入力 / 出力先固定 / 完了時フォルダ自動オープン /
     やさしい進捗・エラー / 設定の永続化

依存: customtkinter（必須）, Pillow（任意・表紙プレビュー用）
"""

import io
import os
import re
import sys
import json
import queue
import threading
import subprocess
import tempfile
from pathlib import Path

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
from alphapolis_search import AlphaPolisSearchClient, SearchFilters, validate_filters

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
except Exception as e:  # pragma: no cover - 起動環境依存
    sys.stderr.write(
        "customtkinter が必要です: pip install customtkinter\n"
        f"  詳細: {e}\n"
    )
    raise

# ══════════════════════════════════════════
#  定数
# ══════════════════════════════════════════
APP_NAME      = "小説ePubダウンローダー"
APP_DIR_NAME  = "NovelDownloader"          # %APPDATA%\NovelDownloader
ICON_FILENAME = "novel_downloader.ico"
SETTINGS_SCHEMA = 3

IS_WINDOWS = (os.name == "nt")
# 子プロセスに黒いコンソール窓を出さない（Windows のみ）
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# 進捗行: 先頭空白必須（ステージ見出し [1/3] を弾く）／[N/M] を抽出（gui_v1_design §9.2）
_RE_PROGRESS = re.compile(r"^\s+\[\s*(\d+)\s*/\s*(\d+)\s*\]")
# 完了時の epub パス行（gui_v1_design §6 / §9.2）
_RE_EPUB_DONE = re.compile(r"✅\s*ePub出力完了:\s*(.+)$")
_RE_TXT_DONE = re.compile(
    r"✅\s*テキスト出力完了:\s*(.+\.txt)(?:\s+（.*)?$", re.IGNORECASE
)
_RE_SPLIT_DONE = re.compile(r"✅\s*分割テキスト出力完了:\s*(.+)$")
_RE_URL = re.compile(r"https?://[^\s'\"<>]+")

ENCODING_CHOICES = ["utf-8", "utf-8-sig", "shift_jis", "cp932"]
LIBRARY_MODE_LABELS = {"manual": "手动筛选", "auto": "自动筛选"}
LIBRARY_MODE_VALUES = {label: value for value, label in LIBRARY_MODE_LABELS.items()}
_STATUS_LABELS = {
    "new": "新书",
    "recorded": "已收录",
    "batch_duplicate": "本批重复",
    "inspect_failed": "检查失败",
}
ALPHAPOLIS_CATEGORIES = {
    "悬疑": "110100", "恐怖": "110200", "科幻": "110300",
    "奇幻": "110400", "恋爱": "110500", "青春": "110600",
    "现代文学": "110700", "大众娱乐": "110800", "经济・企业": "111000",
    "历史・时代": "111100", "儿童书・童话": "111200", "绘本": "111300",
    "角色文艺": "111400", "轻文学": "111500", "随笔・非虚构": "111600",
    "BL": "119000",
}
ALPHAPOLIS_SORTS = {"收藏数": "favorite", "更新时间": "update", "最新发布": "new"}
_CATEGORY_BY_ID = {value: key for key, value in ALPHAPOLIS_CATEGORIES.items()}
_SORT_BY_VALUE = {value: key for key, value in ALPHAPOLIS_SORTS.items()}


# ══════════════════════════════════════════
#  パス解決（gui_v1_design §12.2）
# ══════════════════════════════════════════
def _app_base_dir() -> str:
    """凍結時は exe のあるディレクトリ、開発時はこのスクリプトのディレクトリ。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource_path(name: str) -> str:
    """同梱リソース（アイコン等）の絶対パス。onefile は sys._MEIPASS に展開される。"""
    base = getattr(sys, "_MEIPASS", _app_base_dir())
    return os.path.join(base, name)


def engine_cmd(*cli_args) -> list:
    """エンジン（CLI）を起動するコマンド配列を返す。

    凍結時: 隣の novel_downloader.exe を呼ぶ。
    開発時: python で novel_downloader.py を呼ぶ。
    """
    if getattr(sys, "frozen", False):
        exe = os.path.join(_app_base_dir(), "novel_downloader.exe")
        return [exe, *cli_args]
    script = os.path.join(_app_base_dir(), "novel_downloader.py")
    return [sys.executable, script, *cli_args]


def _engine_env() -> dict:
    """エンジン起動用の環境変数。ライブ進捗と UTF-8 出力を保証（§9.3 / §2.3）。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"   # ライブ進捗（バッファさせない）
    env["PYTHONUTF8"] = "1"         # 日本語出力を UTF-8 に固定
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def default_output_dir() -> str:
    """既定保存先: ダウンロード\\小説（gui_v1_design §3.1）。"""
    if IS_WINDOWS:
        home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    else:
        home = os.path.expanduser("~")
    return os.path.join(home, "Downloads", "小説")


def _default_library_path() -> Path:
    """返回与原书库筛查程序共用的数据文件。"""
    workspace = Path(_app_base_dir()).resolve().parent.parent
    return workspace / "书库筛查" / "书库筛查数据" / "书库数据.json"


def _build_inspect_args(
    target_url: str, settings: dict, site: str = ""
) -> list[str]:
    """构造不产生任何输出文件的作品信息预检参数。"""
    if site == "aozora":
        return ["--inspect-metadata", target_url]
    return [target_url, "--dry-run", "--delay", str(settings["delay"])]


def _selected_candidates(
    candidates: list[Candidate], mode: str
) -> list[Candidate]:
    """根据筛选模式返回真正进入正文下载的候选。"""
    if mode == "auto":
        return [item for item in candidates if item.status == "new"]
    return [
        item
        for item in candidates
        if item.selected and item.status != "inspect_failed"
    ]


def _status_text(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _status_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts = {status: 0 for status in _STATUS_LABELS}
    for item in candidates:
        if item.status in counts:
            counts[item.status] += 1
    return counts


def _format_summary(summary: dict[str, int]) -> str:
    """生成批处理结束时固定、易核对的中文汇总。"""
    return (
        "本批处理完成\n"
        f"成功下载 {summary.get('downloaded', 0)}｜"
        f"已收录 {summary.get('recorded_skipped', 0)}｜"
        f"本批重复 {summary.get('batch_skipped', 0)}\n"
        f"预检失败 {summary.get('inspect_failed', 0)}｜"
        f"下载失败 {summary.get('download_failed', 0)}｜"
        f"记录失败 {summary.get('record_failed', 0)}"
    )


def _format_alphapolis_summary(summary: dict) -> str:
    reasons = {
        "target_met": "已达到目标", "results_exhausted": "筛选结果已用尽",
        "search_error": "搜索请求失败", "library_error": "书库无法读取",
    }
    return (
        f"AlphaPolis 批量下载完成（{reasons.get(summary.get('stop_reason'), '已结束')}）\n"
        f"目标 {summary.get('target', 0)}｜实际成功 {summary.get('downloaded', 0)}｜"
        f"已扫描 {summary.get('pages_scanned', 0)} 页\n"
        f"已收录 {summary.get('recorded_skipped', 0)}｜本批重复 {summary.get('batch_skipped', 0)}｜"
        f"预检失败 {summary.get('inspect_failed', 0)}｜下载失败 {summary.get('download_failed', 0)}"
    )


def _batch_target_from_text(value: str) -> int:
    try:
        target = int(value.strip())
    except (AttributeError, ValueError):
        target = 0
    if target <= 0:
        raise ValueError("下载数量必须是大于 0 的整数")
    return target


def _batch_filters_from_values(category_label: str, complete_only: bool,
                               char_min_text: str, char_max_text: str,
                               sort_label: str) -> SearchFilters:
    try:
        char_min = int(char_min_text.strip()) if char_min_text.strip() else None
        char_max = int(char_max_text.strip()) if char_max_text.strip() else None
    except (AttributeError, ValueError) as exc:
        raise ValueError("字数必须填写整数") from exc
    category_id = ALPHAPOLIS_CATEGORIES.get(category_label)
    sort = ALPHAPOLIS_SORTS.get(sort_label)
    if not category_id or not sort:
        raise ValueError("题材或排序方式无效")
    filters = SearchFilters(category_id, bool(complete_only), char_min, char_max, sort)
    validate_filters(filters)
    return filters


# ══════════════════════════════════════════
#  設定の永続化（gui_v1_design §13）
# ══════════════════════════════════════════
def settings_path() -> str:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", _app_base_dir())
        return os.path.join(base, APP_DIR_NAME, "settings.json")
    cfg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(cfg, "novel_downloader_gui", "settings.json")


def default_settings() -> dict:
    return {
        "schema": SETTINGS_SCHEMA,
        "output_dir": default_output_dir(),
        "cover_mode": "auto",          # "auto" | "site" | "file"
        "cover_image_path": "",
        "horizontal": False,
        "kobo": False,
        "toc_at_end": False,
        "font_path": "",
        "delay": 1.5,
        "encoding": "utf-8",
        "output_mode": "normal",
        "library_mode": "manual",
        "alphapolis_batch": {
            "category_id": "110500",
            "complete_only": True,
            "char_min": 19000,
            "char_max": 50000,
            "sort": "favorite",
            "target_count": 100,
        },
    }


def load_settings() -> dict:
    """壊れていても既定で起動。欠損キーは既定で補完（§13.3）。"""
    s = default_settings()
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("schema") in (1, 2, SETTINGS_SCHEMA):
            for k in s:
                if k in data:
                    s[k] = data[k]
    except Exception:
        pass  # 無い／壊れている → 既定のまま
    # 妥当性
    if not s.get("output_dir"):
        s["output_dir"] = default_output_dir()
    if s.get("cover_mode") == "file" and not (
        s.get("cover_image_path") and os.path.isfile(s["cover_image_path"])
    ):
        s["cover_mode"] = "auto"       # 画像が無ければ auto へフォールバック（§13.3）
    if s.get("font_path") and not os.path.isfile(s["font_path"]):
        s["font_path"] = ""            # フォントファイルが無ければ埋め込みなしへ
    try:
        s["delay"] = float(s.get("delay", 1.5))
    except Exception:
        s["delay"] = 1.5
    if s.get("encoding") not in ENCODING_CHOICES:
        s["encoding"] = "utf-8"
    if s.get("output_mode") not in ("normal", "clean_txt", "split_txt"):
        s["output_mode"] = "normal"
    if s.get("library_mode") not in ("manual", "auto"):
        s["library_mode"] = "manual"
    batch = s.get("alphapolis_batch")
    if not isinstance(batch, dict):
        batch = default_settings()["alphapolis_batch"]
    defaults = default_settings()["alphapolis_batch"]
    batch = {**defaults, **{k: batch[k] for k in defaults if k in batch}}
    try:
        filters = SearchFilters(
            str(batch["category_id"]), bool(batch["complete_only"]),
            batch["char_min"], batch["char_max"], str(batch["sort"]),
        )
        validate_filters(filters)
        _batch_target_from_text(str(batch["target_count"]))
    except (TypeError, ValueError):
        batch = defaults
    s["alphapolis_batch"] = batch
    s["schema"] = SETTINGS_SCHEMA
    return s


def save_settings(s: dict) -> None:
    """アトミック書き込み（tempfile + os.replace, §13.3）。"""
    try:
        p = settings_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass  # 保存失敗は致命ではない


# ══════════════════════════════════════════
#  エンジン呼び出し（読み取り専用モード）
# ══════════════════════════════════════════
def _run_capture(cli_args, timeout=30) -> str:
    """エンジンを起動し stdout を文字列で返す（--detect-site / --list-sites 用）。"""
    proc = subprocess.run(
        engine_cmd(*cli_args),
        capture_output=True, env=_engine_env(),
        creationflags=_CREATE_NO_WINDOW, timeout=timeout,
    )
    return proc.stdout.decode("utf-8", "replace")


def detect_site(url: str):
    """--detect-site を呼び結果 dict を返す。失敗時 None。"""
    try:
        out = _run_capture(["--detect-site", url])
        line = [ln for ln in out.splitlines() if ln.strip()][-1]  # JSON は最終行
        return json.loads(line)
    except Exception:
        return None


def list_sites():
    """--list-sites を呼び [{site, display_name}] を返す。失敗時 []。"""
    try:
        out = _run_capture(["--list-sites"])
        line = [ln for ln in out.splitlines() if ln.strip()][-1]
        return json.loads(line)
    except Exception:
        return []


# ══════════════════════════════════════════
#  メインアプリ
# ══════════════════════════════════════════
class NovelDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._proc = None              # 実行中のダウンロードプロセス
        self._worker = None
        self._abort_event = threading.Event()  # 中止フラグ（事前チェック中の中止にも対応）
        self._queue = queue.Queue()
        self._epub_path = None         # 完了時に開く epub
        self._raw_log = []             # 生ログ（詳細表示用）
        self._detail_open = False
        self._log_open = False

        self.title(APP_NAME)
        self.geometry("590x500")
        self.minsize(520, 360)
        try:
            ico = _resource_path(ICON_FILENAME)
            if IS_WINDOWS and os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        ctk.set_appearance_mode("system")
        self._build_widgets()
        self._apply_settings_to_widgets()
        self._set_state_idle()

        # 起動時クリップボード自動入力（別スレッドで判定・§7.2）
        self._start_clipboard_autofill()
        # キュー監視
        self.after(100, self._poll_queue)
        # 終了時に設定保存
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── ウィジェット構築（gui_v1_design §10） ──────────────────
    def _build_widgets(self):
        self.grid_columnconfigure(0, weight=1)

        # URL 欄
        input_head = ctk.CTkFrame(self, fg_color="transparent")
        input_head.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 2))
        input_head.grid_columnconfigure(0, weight=1)
        self.lbl_url = ctk.CTkLabel(input_head, text="小説のURLを貼り付け", anchor="w")
        self.lbl_url.grid(row=0, column=0, sticky="ew")
        self.btn_batch = ctk.CTkButton(input_head, text="複数リンク / TXTを読み込む",
                                       width=180, command=self._open_batch_dialog)
        self.btn_batch.grid(row=0, column=1)

        self.var_url = ctk.StringVar()
        self.ent_url = ctk.CTkEntry(self, textvariable=self.var_url,
                                    placeholder_text="ここにURLを貼り付けてください…")
        self.ent_url.grid(row=1, column=0, sticky="ew", padx=20)
        self.var_url.trace_add("write", lambda *_: self._update_download_enabled())

        # 书库筛选模式 + 大按钮（下载 / 中止）
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.grid(row=2, column=0, sticky="ew", padx=20, pady=14)
        action.grid_columnconfigure(0, weight=1)
        self.btn_alphapolis_batch = ctk.CTkButton(
            action, text="AlphaPolis 批量筛选下载", command=self._open_alphapolis_batch_dialog
        )
        self.btn_alphapolis_batch.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.var_library_mode = ctk.StringVar(value=LIBRARY_MODE_LABELS["manual"])
        self.mode_switch = ctk.CTkSegmentedButton(
            action,
            values=[LIBRARY_MODE_LABELS["manual"], LIBRARY_MODE_LABELS["auto"]],
            variable=self.var_library_mode,
            command=lambda *_: self._persist(),
        )
        self.mode_switch.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.btn_main = ctk.CTkButton(action, text="⬇ ダウンロード", height=44,
                                      font=ctk.CTkFont(size=16, weight="bold"),
                                      command=self._on_main_button)
        self.btn_main.grid(row=2, column=0, sticky="ew")

        # ステータス行（進捗テキスト / 完了 / エラー）
        self.lbl_status = ctk.CTkLabel(self, text="", anchor="w", justify="left")
        self.lbl_status.grid(row=3, column=0, sticky="ew", padx=20)

        # 進捗バー
        self.bar = ctk.CTkProgressBar(self)
        self.bar.grid(row=4, column=0, sticky="ew", padx=20, pady=(4, 2))
        self.bar.set(0)
        self.bar.grid_remove()

        # 補助ボタン行（フォルダを開く / 対応サイト / 詳細を表示）
        self.frm_aux = ctk.CTkFrame(self, fg_color="transparent")
        self.frm_aux.grid(row=5, column=0, sticky="ew", padx=20, pady=2)
        self.frm_aux.grid_remove()   # 空のときは隠す（CTkFrame の既定サイズで居座らせない）
        self.btn_open = ctk.CTkButton(self.frm_aux, text="📂 フォルダを開く",
                                      width=140, command=self._open_folder)
        self.btn_sites = ctk.CTkButton(self.frm_aux, text="対応サイトを見る",
                                       width=140, fg_color="gray40",
                                       command=self._show_sites)
        self.btn_log = ctk.CTkButton(self.frm_aux, text="詳細を表示",
                                     width=110, fg_color="gray30",
                                     command=self._toggle_log)

        # 保存先表示（小）
        self.lbl_outdir = ctk.CTkLabel(self, text="", anchor="w",
                                       text_color="gray", font=ctk.CTkFont(size=11))
        self.lbl_outdir.grid(row=6, column=0, sticky="ew", padx=20, pady=(6, 0))

        # 詳細設定トグル
        self.btn_detail = ctk.CTkButton(
            self, text="▸ 詳細設定（保存先・表紙などの変更）", anchor="w",
            fg_color=("gray90", "gray25"), text_color=("gray10", "gray90"),
            hover_color=("gray80", "gray35"), cursor="hand2",
            command=self._toggle_detail)
        self.btn_detail.grid(row=7, column=0, sticky="ew", padx=16, pady=(4, 0))

        # 詳細設定パネル（§10.2）
        self._build_detail_panel()

        # 生ログ（§10.1 トグル先）
        self.txt_log = ctk.CTkTextbox(self, height=120)

    def _build_detail_panel(self):
        self.frm_detail = ctk.CTkFrame(self)
        self.frm_detail.grid_columnconfigure(0, weight=1)

        # 保存先
        ctk.CTkLabel(self.frm_detail, text="保存先", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        row = ctk.CTkFrame(self.frm_detail, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=12)
        row.grid_columnconfigure(0, weight=1)
        self.var_outdir = ctk.StringVar()
        self.ent_outdir = ctk.CTkEntry(row, textvariable=self.var_outdir)
        self.ent_outdir.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row, text="変更", width=60,
                      command=self._pick_output_dir).grid(row=0, column=1, padx=(8, 0))

        # 表紙（排他ラジオ・§10.2）
        ctk.CTkLabel(self.frm_detail, text="表紙（ePubの“顔”）", anchor="w").grid(
            row=2, column=0, sticky="ew", padx=12, pady=(12, 0))
        self.var_cover = ctk.StringVar(value="auto")
        for i, (val, label) in enumerate([
            ("auto", "おまかせ（自動で作る）"),
            ("site", "サイトの公式表紙を使う"),
            ("file", "自分の画像を選ぶ…"),
        ]):
            ctk.CTkRadioButton(self.frm_detail, text=label, value=val,
                               variable=self.var_cover,
                               command=self._on_cover_change).grid(
                row=3 + i, column=0, sticky="w", padx=24, pady=1)

        # 画像選択行
        self.frm_cover_file = ctk.CTkFrame(self.frm_detail, fg_color="transparent")
        self.frm_cover_file.grid(row=6, column=0, sticky="ew", padx=24)
        self.frm_cover_file.grid_columnconfigure(0, weight=1)
        self.var_cover_file = ctk.StringVar(value="画像が未選択です")
        self.lbl_cover_file = ctk.CTkLabel(self.frm_cover_file,
                                           textvariable=self.var_cover_file,
                                           anchor="w", text_color="gray")
        self.lbl_cover_file.grid(row=0, column=0, sticky="ew")
        self.btn_cover_pick = ctk.CTkButton(self.frm_cover_file, text="画像を選ぶ",
                                            width=90, command=self._pick_cover_image)
        self.btn_cover_pick.grid(row=0, column=1, padx=(8, 0))
        self._cover_image_path = ""

        # 区切り線
        sep = ctk.CTkFrame(self.frm_detail, height=1, fg_color="gray70")
        sep.grid(row=7, column=0, sticky="ew", padx=12, pady=10)
        ctk.CTkLabel(self.frm_detail, text="ここから下は普段は変更不要",
                     text_color="gray", font=ctk.CTkFont(size=11)).grid(
            row=8, column=0, sticky="w", padx=12)

        # 下段オプション
        opt = ctk.CTkFrame(self.frm_detail, fg_color="transparent")
        opt.grid(row=9, column=0, sticky="ew", padx=12, pady=(2, 12))
        self.var_horizontal = ctk.BooleanVar(value=False)
        self.var_kobo = ctk.BooleanVar(value=False)
        self.var_toc_at_end = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opt, text="横書きにする", variable=self.var_horizontal,
                        command=self._persist).grid(row=0, column=0, sticky="w", pady=2)
        ctk.CTkCheckBox(opt, text="Kobo端末向け (.kepub.epub)", variable=self.var_kobo,
                        command=self._persist).grid(row=0, column=1, sticky="w", padx=(16, 0), pady=2)
        ctk.CTkCheckBox(opt, text="目次を本の最後に置く", variable=self.var_toc_at_end,
                        command=self._persist).grid(row=1, column=0, columnspan=2,
                                                    sticky="w", pady=2)

        # 本文フォント（ePub に埋め込むフォント・見た目に直結）
        ctk.CTkLabel(opt, text="本文のフォント").grid(row=2, column=0, sticky="w", pady=(6, 0))
        fontbox = ctk.CTkFrame(opt, fg_color="transparent")
        fontbox.grid(row=2, column=1, sticky="w", padx=(16, 0), pady=(6, 0))
        self._font_path = ""
        self.var_font_name = ctk.StringVar(value="標準（埋め込みなし）")
        ctk.CTkLabel(fontbox, textvariable=self.var_font_name,
                     text_color="gray").pack(side="left")
        ctk.CTkButton(fontbox, text="選ぶ", width=56,
                      command=self._pick_font).pack(side="left", padx=(8, 0))
        ctk.CTkButton(fontbox, text="標準に戻す", width=84, fg_color="gray40",
                      command=self._clear_font).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(opt, text="取得間隔（秒）").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.var_delay = ctk.StringVar(value="1.5")
        ctk.CTkEntry(opt, textvariable=self.var_delay, width=70).grid(
            row=3, column=1, sticky="w", padx=(16, 0), pady=(6, 0))
        ctk.CTkLabel(opt, text="文字コード").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.var_encoding = ctk.StringVar(value="utf-8")
        ctk.CTkOptionMenu(opt, values=ENCODING_CHOICES, variable=self.var_encoding,
                          width=130, command=lambda *_: self._persist()).grid(
            row=4, column=1, sticky="w", padx=(16, 0), pady=(6, 0))
        ctk.CTkLabel(opt, text="出力モード").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.var_output_mode = ctk.StringVar(value="通常（EPUB + TXT）")
        ctk.CTkOptionMenu(
            opt,
            values=["通常（EPUB + TXT）", "純粋TXTのみ（本文だけ）",
                    "純粋TXT分割（同名フォルダ・約2万字）"],
            variable=self.var_output_mode,
            width=210,
            command=lambda *_: self._persist(),
        ).grid(row=5, column=1, sticky="w", padx=(16, 0), pady=(6, 0))

    # ── 設定 ⇔ ウィジェット ───────────────────────────────────
    def _apply_settings_to_widgets(self):
        s = self.settings
        self.var_outdir.set(s["output_dir"])
        self.var_cover.set(s["cover_mode"])
        self._cover_image_path = s.get("cover_image_path", "")
        if self._cover_image_path:
            self.var_cover_file.set(os.path.basename(self._cover_image_path))
        self.var_horizontal.set(bool(s["horizontal"]))
        self.var_kobo.set(bool(s["kobo"]))
        self.var_toc_at_end.set(bool(s.get("toc_at_end", False)))
        self._font_path = s.get("font_path", "")
        self.var_font_name.set(os.path.basename(self._font_path)
                               if self._font_path else "標準（埋め込みなし）")
        self.var_delay.set(str(s["delay"]))
        self.var_encoding.set(s["encoding"])
        mode_labels = {
            "normal": "通常（EPUB + TXT）",
            "clean_txt": "純粋TXTのみ（本文だけ）",
            "split_txt": "純粋TXT分割（同名フォルダ・約2万字）",
        }
        self.var_output_mode.set(mode_labels.get(s.get("output_mode"), mode_labels["normal"]))
        self.var_library_mode.set(LIBRARY_MODE_LABELS[s.get("library_mode", "manual")])
        self.lbl_outdir.configure(text=f"保存先： {s['output_dir']}")
        self._on_cover_change()

    def _collect_settings(self) -> dict:
        try:
            delay = float(self.var_delay.get())
        except Exception:
            delay = 1.5
        return {
            "schema": SETTINGS_SCHEMA,
            "output_dir": self.var_outdir.get().strip() or default_output_dir(),
            "cover_mode": self.var_cover.get(),
            "cover_image_path": self._cover_image_path,
            "horizontal": bool(self.var_horizontal.get()),
            "kobo": bool(self.var_kobo.get()),
            "toc_at_end": bool(self.var_toc_at_end.get()),
            "font_path": self._font_path,
            "delay": delay,
            "encoding": self.var_encoding.get(),
            "output_mode": (
                "split_txt" if "分割" in self.var_output_mode.get()
                else "clean_txt" if self.var_output_mode.get().startswith("純粋")
                else "normal"),
            "library_mode": LIBRARY_MODE_VALUES.get(
                self.var_library_mode.get(), "manual"),
            "alphapolis_batch": self.settings.get(
                "alphapolis_batch", default_settings()["alphapolis_batch"]),
        }

    def _persist(self):
        self.settings = self._collect_settings()
        self.lbl_outdir.configure(text=f"保存先： {self.settings['output_dir']}")
        save_settings(self.settings)

    # ── 状態遷移（§4） ───────────────────────────────────────
    def _hide_aux(self):
        for b in (self.btn_open, self.btn_sites, self.btn_log):
            b.grid_forget()
        self.frm_aux.grid_remove()   # 空のフレームが高さを占有しないよう隠す

    def _set_state_idle(self):
        self.btn_main.configure(text="⬇ ダウンロード", state="normal")
        self.mode_switch.configure(state="normal")
        self.btn_batch.configure(state="normal")
        self.btn_alphapolis_batch.configure(state="normal")
        self.lbl_status.configure(text="", text_color=("gray10", "gray90"))
        self.bar.grid_remove()
        self.bar.stop()
        self._hide_aux()
        self.ent_url.configure(state="normal")
        self._update_download_enabled()

    def _set_state_running(self):
        self.btn_main.configure(text="⏸ 中止", state="normal")
        self.mode_switch.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.btn_alphapolis_batch.configure(state="disabled")
        self.ent_url.configure(state="disabled")
        self.lbl_status.configure(text="準備中…", text_color=("gray10", "gray90"))
        self.bar.grid()
        self.bar.configure(mode="indeterminate")
        self.bar.start()
        self._hide_aux()

    def _set_state_done(self):
        self.btn_main.configure(text="⬇ ダウンロード", state="normal")
        self.mode_switch.configure(state="normal")
        self.btn_batch.configure(state="normal")
        self.btn_alphapolis_batch.configure(state="normal")
        self.ent_url.configure(state="normal")
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.bar.set(1)
        name = os.path.basename(self._epub_path) if self._epub_path else "ファイル"
        self.lbl_status.configure(text=f"✅ 完了しました！  「{name}」を保存しました",
                                  text_color=("#1a7f37", "#3fb950"))
        self._hide_aux()
        self.frm_aux.grid()
        self.btn_open.grid(row=0, column=0, padx=(0, 8))

    def _set_state_error(self, kind: str):
        """kind: 'unsupported' | 'hameln' | 'failed'"""
        self.btn_main.configure(text="⬇ もう一度", state="normal")
        self.mode_switch.configure(state="normal")
        self.btn_batch.configure(state="normal")
        self.btn_alphapolis_batch.configure(state="normal")
        self.ent_url.configure(state="normal")
        self.bar.stop()
        self.bar.grid_remove()
        self._hide_aux()
        if kind == "unsupported":
            msg = ("⚠ このサイトには対応していません\n"
                   "URLが正しいか、対応しているサイトかをご確認ください。")
            self.frm_aux.grid()
            self.btn_sites.grid(row=0, column=0, padx=(0, 8))
        elif kind == "hameln":
            msg = ("⚠ ハーメルンには対応していません\n"
                   "申し訳ありませんが、別のサイトのURLでお試しください。")
        else:
            msg = ("⚠ うまくいきませんでした\n"
                   "通信状態を確認して、もう一度お試しください。")
            self.frm_aux.grid()
            self.btn_log.grid(row=0, column=0, padx=(0, 8))
        self.lbl_status.configure(text=msg, text_color=("#b3261e", "#f2b8b5"))

    def _update_download_enabled(self):
        if self._proc is not None:
            return
        has_url = bool(self.var_url.get().strip())
        self.btn_main.configure(state=("normal" if has_url else "disabled"))

    # ── 大ボタン（ダウンロード / 中止） ─────────────────────────
    def _on_main_button(self):
        if self._proc is not None:        # 実行中 → 中止
            self._abort()
            return
        url = self.var_url.get().strip()
        if not url:
            return
        self._persist()
        self._launch_inspection(url, self._collect_settings())

    def _launch_inspection(self, text: str, s: dict) -> bool:
        """让单网址输入和批量输入共用同一个预检入口。"""
        if self._proc is not None:
            return False
        self._epub_path = None
        self._raw_log = []
        self._abort_event.clear()
        self._set_state_running()
        self._proc = "starting"           # 二重起動防止のプレースホルダ
        self._worker = threading.Thread(target=self._inspection_worker,
                                        args=(text, s), daemon=True)
        self._worker.start()
        return True

    def _launch_alphapolis_batch(self, filters: SearchFilters, target_count: int,
                                 s: dict) -> bool:
        if self._proc is not None:
            return False
        self._epub_path = None
        self._raw_log = []
        self._abort_event.clear()
        self._set_state_running()
        self._proc = "starting"
        self._worker = threading.Thread(
            target=self._alphapolis_batch_worker,
            args=(filters, target_count, s), daemon=True,
        )
        self._worker.start()
        return True

    def _inspect_url_candidate(self, url: str, s: dict) -> Candidate:
        info = detect_site(url)
        if not info or info.get("site") is None:
            return Candidate(url, url, "", error="不支持该网站或网址格式不正确")
        target_url = info.get("normalized_url") or url
        site_name = info.get("display_name") or info.get("site") or ""
        if info.get("needs_playwright"):
            return Candidate(url, target_url, site_name, error="当前图形界面暂不支持该网站")
        try:
            proc = subprocess.Popen(
                engine_cmd(*_build_inspect_args(target_url, s, info.get("site", ""))),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_engine_env(),
                creationflags=_CREATE_NO_WINDOW, universal_newlines=True,
                encoding="utf-8", errors="replace",
            )
            self._proc = proc
            output, _ = proc.communicate()
            if self._abort_event.is_set():
                return Candidate(url, target_url, site_name, error="已取消")
            title = parse_dry_run_title(output or "") if proc.returncode == 0 else ""
            if title:
                return Candidate(url, target_url, site_name, title, safe_library_name(title))
            lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
            return Candidate(url, target_url, site_name,
                             error=lines[-1] if lines else "无法取得作品标题")
        except Exception as exc:
            return Candidate(url, target_url, site_name, error=f"预检启动失败：{exc}")

    def _download_candidate(self, candidate: Candidate, s: dict) -> tuple[bool, str]:
        reported_txt_path = ""
        try:
            args = self._build_cli_args(candidate.normalized_url, s)
            args += ["-o", Path(candidate.library_name).stem]
            proc = subprocess.Popen(
                engine_cmd(*args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=_engine_env(), creationflags=_CREATE_NO_WINDOW, bufsize=1,
                universal_newlines=True, encoding="utf-8", errors="replace",
            )
            self._proc = proc
            for line in proc.stdout:
                line = line.rstrip("\n")
                self._queue.put(("rawlog", line))
                mt = _RE_TXT_DONE.search(line) or _RE_SPLIT_DONE.search(line)
                if mt:
                    reported_txt_path = mt.group(1).strip()
                    self._queue.put(("output", reported_txt_path))
            return proc.wait() == 0 and verify_txt_output(reported_txt_path), reported_txt_path
        except Exception as exc:
            self._queue.put(("rawlog", f"[下载异常] {candidate.title or candidate.url}: {exc}"))
            return False, ""

    def _alphapolis_batch_worker(self, filters: SearchFilters, target_count: int,
                                 s: dict) -> None:
        summary = {"target": target_count, "downloaded": 0, "pages_scanned": 0,
                   "checked": 0, "recorded_skipped": 0, "batch_skipped": 0,
                   "inspect_failed": 0, "download_failed": 0, "record_failed": 0,
                   "stop_reason": "results_exhausted"}
        seen_names: set[str] = set()
        try:
            pages = AlphaPolisSearchClient(s.get("delay", 1.5)).iter_pages(
                filters, self._abort_event.is_set)
            for page in pages:
                if self._abort_event.is_set():
                    self._queue.put(("aborted",))
                    return
                summary["pages_scanned"] += 1
                for url in page.novel_urls:
                    if self._abort_event.is_set():
                        self._queue.put(("aborted",))
                        return
                    candidate = self._inspect_url_candidate(url, s)
                    summary["checked"] += 1
                    self._queue.put(("search_status", summary["pages_scanned"],
                                     summary["checked"], summary["downloaded"],
                                     target_count, candidate.title or url))
                    if candidate.error:
                        summary["inspect_failed"] += 1
                        self._queue.put(("rawlog", f"[预检失败] {url}: {candidate.error}"))
                        continue
                    try:
                        library = load_library(_default_library_path())
                    except LibraryDataError as exc:
                        summary["stop_reason"] = "library_error"
                        self._queue.put(("rawlog", f"[书库错误] {exc}"))
                        self._queue.put(("alphapolis_finished", summary))
                        return
                    classify_candidates([candidate], set(library["recorded_names"]) | seen_names)
                    if candidate.status == "recorded":
                        summary["recorded_skipped"] += 1
                        continue
                    if candidate.status == "batch_duplicate":
                        summary["batch_skipped"] += 1
                        continue
                    ok, _path = self._download_candidate(candidate, s)
                    if self._abort_event.is_set():
                        self._queue.put(("aborted",))
                        return
                    if not ok:
                        summary["download_failed"] += 1
                        continue
                    try:
                        record_library_name(_default_library_path(), candidate.library_name)
                    except Exception as exc:
                        summary["record_failed"] += 1
                        self._queue.put(("rawlog", f"[书库记录失败] {candidate.title}: {exc}"))
                    seen_names.add(candidate.library_name)
                    summary["downloaded"] += 1
                    if summary["downloaded"] >= target_count:
                        summary["stop_reason"] = "target_met"
                        self._queue.put(("alphapolis_finished", summary))
                        return
        except Exception as exc:
            summary["stop_reason"] = "search_error"
            self._queue.put(("rawlog", f"[搜索失败] {exc}"))
        self._queue.put(("alphapolis_finished", summary))

    def _abort(self):
        self._abort_event.set()           # ワーカーが起動前チェックで参照する
        proc = self._proc
        if hasattr(proc, "terminate"):     # 実プロセス起動済みなら停止（finished で中止判定）
            try:
                proc.terminate()
            except Exception:
                pass

    # ── ダウンロードワーカー（別スレッド・§6） ──────────────────
    def _build_cli_args(self, target_url: str, s: dict) -> list:
        args = [target_url, "--output-dir", s["output_dir"]]
        if s["cover_mode"] == "site":
            args.append("--use-site-cover")
        elif s["cover_mode"] == "file" and s.get("cover_image_path") and \
                os.path.isfile(s["cover_image_path"]):
            args += ["--cover-image", s["cover_image_path"]]
        if s["horizontal"]:
            args.append("--horizontal")
        if s["kobo"]:
            args.append("--kobo")
        if s.get("toc_at_end"):
            args.append("--toc-at-end")
        if s.get("font_path") and os.path.isfile(s["font_path"]):
            args += ["--font", s["font_path"]]
        args += ["--delay", str(s["delay"]), "--encoding", s["encoding"]]
        if s.get("output_mode") == "clean_txt":
            args += ["--clean-text", "--no-epub"]
        elif s.get("output_mode") == "split_txt":
            args += ["--split-text", "--no-epub"]
        return args

    @staticmethod
    def _extract_urls(text: str) -> list:
        urls = []
        for match in _RE_URL.finditer(text):
            url = match.group(0).rstrip(".,;:!?)]}、。，；：！？）】」』")
            # AlphaPolis の分類・タグURLを除き、作品トップURLだけを採用。
            if "alphapolis.co.jp" in url and not re.search(
                    r"alphapolis\.co\.jp/novel/\d+/\d+(?:[/?#]|$)", url):
                continue
            urls.append(url)
        return urls

    def _inspection_worker(self, text: str, s: dict):
        """只获取作品标题并在正文下载前完成书库分类。"""
        try:
            library = load_library(_default_library_path())
        except LibraryDataError as exc:
            self._queue.put(("library_error", str(exc)))
            return

        urls = self._extract_urls(text)
        if not urls:
            self._queue.put(("precheck", "unsupported"))
            return

        candidates: list[Candidate] = []
        for index, url in enumerate(urls, 1):
            if self._abort_event.is_set():
                self._queue.put(("aborted",))
                return
            self._queue.put(("inspection_progress", index, len(urls), url))
            info = detect_site(url)
            if not info or info.get("site") is None:
                candidates.append(Candidate(
                    url, url, "", error="不支持该网站或网址格式不正确"
                ))
                continue
            target_url = info.get("normalized_url") or url
            site_name = info.get("display_name") or info.get("site") or ""
            if info.get("needs_playwright"):
                candidates.append(Candidate(
                    url, target_url, site_name, error="当前图形界面暂不支持该网站"
                ))
                continue
            try:
                proc = subprocess.Popen(
                    engine_cmd(*_build_inspect_args(target_url, s, info.get("site", ""))),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=_engine_env(),
                    creationflags=_CREATE_NO_WINDOW,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self._proc = proc
                if self._abort_event.is_set():
                    proc.terminate()
                output, _ = proc.communicate()
                if self._abort_event.is_set():
                    self._queue.put(("aborted",))
                    return
                title = parse_dry_run_title(output or "") if proc.returncode == 0 else ""
                if title:
                    candidates.append(Candidate(
                        url=url,
                        normalized_url=target_url,
                        site=site_name,
                        title=title,
                        library_name=safe_library_name(title),
                    ))
                else:
                    lines = [line.strip() for line in (output or "").splitlines()
                             if line.strip()]
                    error = lines[-1] if lines else "无法取得作品标题"
                    candidates.append(Candidate(
                        url, target_url, site_name, error=error
                    ))
            except Exception as exc:
                candidates.append(Candidate(
                    url, target_url, site_name, error=f"预检启动失败：{exc}"
                ))

        for candidate in candidates:
            if candidate.error:
                self._queue.put((
                    "rawlog",
                    f"[预检失败] {candidate.title or candidate.url}: {candidate.error}",
                ))
        classify_candidates(candidates, set(library["recorded_names"]))
        self._queue.put(("inspection_ready", candidates, s))

    def _begin_candidate_download(self, candidates: list[Candidate], s: dict):
        """在预检/人工选择后启动候选下载线程。"""
        self._abort_event.clear()
        self._set_state_running()
        self._proc = "starting"
        self._worker = threading.Thread(
            target=self._download_worker,
            args=(candidates, s),
            daemon=True,
        )
        self._worker.start()

    def _show_selection_dialog(self, candidates: list[Candidate], s: dict):
        """显示手动筛选列表，并把用户选择交给下载阶段。"""
        self._proc = "awaiting_selection"
        counts = _status_counts(candidates)
        win = ctk.CTkToplevel(self)
        win.title("下载前书库筛查")
        win.geometry("820x580")
        win.minsize(680, 460)
        win.transient(self)
        win.grab_set()

        summary_text = (
            f"新书 {counts['new']}　已收录 {counts['recorded']}　"
            f"本批重复 {counts['batch_duplicate']}　"
            f"检查失败 {counts['inspect_failed']}"
        )
        ctk.CTkLabel(
            win,
            text="请选择要下载的小说",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 2))
        ctk.CTkLabel(win, text=summary_text, anchor="w", text_color="gray").pack(
            fill="x", padx=18, pady=(0, 10)
        )

        scroll = ctk.CTkScrollableFrame(win)
        scroll.pack(fill="both", expand=True, padx=18, pady=4)
        scroll.grid_columnconfigure(1, weight=1)
        selections: list[tuple[Candidate, ctk.BooleanVar]] = []
        status_colors = {
            "new": ("#1a7f37", "#3fb950"),
            "recorded": ("#9a6700", "#d29922"),
            "batch_duplicate": ("#9a6700", "#d29922"),
            "inspect_failed": ("#b3261e", "#f2b8b5"),
        }
        for row_index, candidate in enumerate(candidates):
            variable = ctk.BooleanVar(value=bool(candidate.selected))
            checkbox = ctk.CTkCheckBox(scroll, text="", width=28, variable=variable)
            checkbox.grid(row=row_index, column=0, padx=(8, 4), pady=7)
            if candidate.status == "inspect_failed":
                checkbox.configure(state="disabled")
            display = candidate.title or candidate.url
            detail = candidate.site
            if candidate.error:
                detail = f"{detail}　{candidate.error}".strip()
            text_frame = ctk.CTkFrame(scroll, fg_color="transparent")
            text_frame.grid(row=row_index, column=1, sticky="ew", pady=5)
            ctk.CTkLabel(text_frame, text=display, anchor="w", justify="left").pack(
                fill="x"
            )
            ctk.CTkLabel(
                text_frame,
                text=detail,
                anchor="w",
                justify="left",
                text_color="gray",
                font=ctk.CTkFont(size=11),
            ).pack(fill="x")
            ctk.CTkLabel(
                scroll,
                text=_status_text(candidate.status),
                text_color=status_colors.get(candidate.status, "gray"),
                width=80,
            ).grid(row=row_index, column=2, padx=10)
            selections.append((candidate, variable))

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(8, 16))

        def select_new():
            for candidate, variable in selections:
                variable.set(candidate.status == "new")

        def clear_all():
            for _candidate, variable in selections:
                variable.set(False)

        def close_dialog():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._proc = None
            self._set_state_idle()

        def download_selected():
            for candidate, variable in selections:
                candidate.selected = bool(variable.get())
            selected = _selected_candidates(candidates, "manual")
            if not selected:
                messagebox.showwarning("没有选择", "请至少选择一本可以下载的小说。", parent=win)
                return
            has_duplicate = any(
                item.status in ("recorded", "batch_duplicate") for item in selected
            )
            if has_duplicate and not messagebox.askyesno(
                "确认下载重复作品",
                "所选重复作品可能覆盖输出目录中的同名文件，仍要继续吗？",
                parent=win,
            ):
                return
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._begin_candidate_download(candidates, s)

        ctk.CTkButton(buttons, text="全选新书", width=100, command=select_new).pack(
            side="left"
        )
        ctk.CTkButton(
            buttons, text="全部取消", width=100, fg_color="gray40", command=clear_all
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            buttons, text="取消", width=90, fg_color="gray40", command=close_dialog
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="下载所选", width=130, command=download_selected
        ).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", close_dialog)

    def _download_worker(self, candidates: list[Candidate], s: dict):
        selected = _selected_candidates(candidates, s.get("library_mode", "manual"))
        selected_ids = {id(item) for item in selected}
        summary = {
            "downloaded": 0,
            "recorded_skipped": sum(
                item.status == "recorded" and id(item) not in selected_ids
                for item in candidates
            ),
            "batch_skipped": sum(
                item.status == "batch_duplicate" and id(item) not in selected_ids
                for item in candidates
            ),
            "inspect_failed": sum(
                item.status == "inspect_failed" for item in candidates
            ),
            "download_failed": 0,
            "record_failed": 0,
        }
        for index, candidate in enumerate(selected, 1):
            if self._abort_event.is_set():
                self._queue.put(("aborted",))
                return
            self._queue.put((
                "batch_status", index, len(selected), candidate.title or candidate.url
            ))
            reported_txt_path = ""
            proc = None
            try:
                download_args = self._build_cli_args(candidate.normalized_url, s)
                download_args += ["-o", Path(candidate.library_name).stem]
                proc = subprocess.Popen(
                    engine_cmd(*download_args),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=_engine_env(), creationflags=_CREATE_NO_WINDOW,
                    bufsize=1, universal_newlines=True, encoding="utf-8", errors="replace",
                )
                self._proc = proc
                if self._abort_event.is_set():
                    proc.terminate()
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    self._queue.put(("rawlog", line))
                    m = _RE_PROGRESS.match(line)
                    if m:
                        self._queue.put(("progress", int(m.group(1)), int(m.group(2))))
                    md = _RE_EPUB_DONE.search(line)
                    if md:
                        self._queue.put(("epub", md.group(1).strip()))
                    mt = _RE_TXT_DONE.search(line)
                    if mt:
                        reported_txt_path = mt.group(1).strip()
                        self._queue.put(("output", reported_txt_path))
                    ms = _RE_SPLIT_DONE.search(line)
                    if ms:
                        reported_txt_path = ms.group(1).strip()
                        self._queue.put(("output", reported_txt_path))
                rc = proc.wait()
                output_valid = verify_txt_output(reported_txt_path)
            except Exception as exc:
                if proc is not None and hasattr(proc, "terminate"):
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                if self._abort_event.is_set():
                    self._queue.put(("aborted",))
                    return
                summary["download_failed"] += 1
                self._queue.put((
                    "rawlog", f"[下载异常] {candidate.title or candidate.url}: {exc}"
                ))
                continue
            if self._abort_event.is_set():
                self._queue.put(("aborted",))
                return
            if rc != 0 or not output_valid:
                summary["download_failed"] += 1
                self._queue.put((
                    "rawlog",
                    f"[下载失败] {candidate.title or candidate.url}: 未确认有效TXT输出",
                ))
                continue
            summary["downloaded"] += 1
            try:
                record_library_name(_default_library_path(), candidate.library_name)
            except Exception as exc:
                summary["record_failed"] += 1
                self._queue.put((
                    "rawlog", f"[书库记录失败] {candidate.title}: {exc}"
                ))
        self._queue.put(("batch_finished", summary))

    # ── キュー監視（UIスレッド・§6） ──────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == "autofill":
            if not self.var_url.get().strip():
                self.var_url.set(msg[1])
            return
        if kind == "progress":
            n, m = msg[1], msg[2]
            if self.bar.cget("mode") != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.set(n / m if m else 0)
            self.lbl_status.configure(text=f"取得中…  第 {n} 話 / 全 {m} 話")
        elif kind == "inspection_progress":
            self.lbl_status.configure(
                text=f"正在检查书库… 作品 {msg[1]} / {msg[2]}"
            )
        elif kind == "search_status":
            self.lbl_status.configure(
                text=f"已扫描 {msg[1]} 页｜已检查 {msg[2]} 本｜成功 {msg[3]}/{msg[4]}\n{msg[5]}"
            )
        elif kind == "epub":
            self._epub_path = msg[1]
        elif kind == "output":
            self._epub_path = msg[1]
        elif kind == "batch_status":
            self.lbl_status.configure(text=f"作品 {msg[1]} / {msg[2]} を処理中…")
        elif kind == "rawlog":
            self._raw_log.append(msg[1])
            if self._log_open:
                self.txt_log.insert("end", msg[1] + "\n")
                self.txt_log.see("end")
        elif kind == "precheck":
            self._proc = None
            self._set_state_error(msg[1])
        elif kind == "library_error":
            self._proc = None
            self._set_state_error("failed")
            self.lbl_status.configure(
                text=f"⚠ 无法安全读取书库\n{_default_library_path()}\n{msg[1]}",
                text_color=("#b3261e", "#f2b8b5"),
            )
        elif kind == "inspection_ready":
            self._proc = None
            candidates, settings = msg[1], msg[2]
            if settings.get("library_mode") == "auto":
                self._begin_candidate_download(candidates, settings)
            else:
                self._show_selection_dialog(candidates, settings)
        elif kind == "aborted":
            self._proc = None
            self._set_state_idle()
            self.lbl_status.configure(text="中止しました。")
        elif kind == "finished":
            rc = msg[1]
            self._proc = None
            if self._abort_event.is_set():   # 中止後の終了は「中止」として扱う
                self._set_state_idle()
                self.lbl_status.configure(text="中止しました。")
                return
            if rc == 0:
                self._fallback_epub_path()
                self._set_state_done()
                self._open_folder()          # 完了と同時に自動オープン
            else:
                self._set_state_error("failed")
        elif kind == "batch_finished":
            summary = msg[1]
            self._proc = None
            if self._abort_event.is_set():
                self._set_state_idle()
                self.lbl_status.configure(text="中止しました。")
            elif summary["downloaded"]:
                self._set_state_done()
                self.lbl_status.configure(
                    text="✅ " + _format_summary(summary),
                    text_color=("#1a7f37", "#3fb950"))
                self._open_folder()
            else:
                self._set_state_idle()
                prefix = "本批没有需要下载的新书\n" if not summary["download_failed"] else ""
                self.lbl_status.configure(
                    text=prefix + _format_summary(summary),
                    text_color=("gray20", "gray80"),
                )
            if summary["inspect_failed"] or summary["download_failed"] or summary["record_failed"]:
                self.frm_aux.grid()
                self.btn_log.grid(row=0, column=0, padx=(0, 8))
        elif kind == "alphapolis_finished":
            summary = msg[1]
            self._proc = None
            if summary["downloaded"]:
                self._set_state_done()
                self.lbl_status.configure(text="✅ " + _format_alphapolis_summary(summary),
                                          text_color=("#1a7f37", "#3fb950"))
                self._open_folder()
            else:
                self._set_state_idle()
                self.lbl_status.configure(text=_format_alphapolis_summary(summary),
                                          text_color=("gray20", "gray80"))
            if (summary["inspect_failed"] or summary["download_failed"]
                    or summary["record_failed"] or summary["stop_reason"] in ("search_error", "library_error")):
                self.frm_aux.grid()
                self.btn_log.grid(row=0, column=0, padx=(0, 8))

    def _fallback_epub_path(self):
        """epub パス未捕捉なら出力先の最新 .epub を採用（§6）。"""
        if self._epub_path and os.path.isfile(self._epub_path):
            return
        try:
            d = self.settings["output_dir"]
            epubs = [os.path.join(d, f) for f in os.listdir(d)
                     if f.lower().endswith((".epub", ".kepub.epub"))]
            if epubs:
                self._epub_path = max(epubs, key=os.path.getmtime)
        except Exception:
            pass

    # ── 各種アクション ────────────────────────────────────────
    def _open_folder(self):
        path = self._epub_path
        try:
            if IS_WINDOWS and path and os.path.isfile(path):
                subprocess.run(["explorer", "/select,", os.path.normpath(path)])
            elif IS_WINDOWS:
                os.startfile(self.settings["output_dir"])  # type: ignore[attr-defined]
            else:
                target = os.path.dirname(path) if path else self.settings["output_dir"]
                subprocess.run(["xdg-open", target])
        except Exception:
            pass

    def _show_sites(self):
        sites = list_sites()
        win = ctk.CTkToplevel(self)
        win.title("対応サイト")
        win.geometry("320x420")
        win.transient(self)
        frm = ctk.CTkScrollableFrame(win, label_text="このアプリが対応しているサイト")
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        if not sites:
            ctk.CTkLabel(frm, text="一覧を取得できませんでした。").pack(anchor="w")
        for s in sites:
            ctk.CTkLabel(frm, text="・" + s.get("display_name", ""),
                         anchor="w").pack(fill="x", anchor="w", pady=1)

    def _toggle_detail(self):
        self._detail_open = not self._detail_open
        if self._detail_open:
            self.btn_detail.configure(text="▾ 詳細設定（保存先・表紙などの変更）")
            self.frm_detail.grid(row=8, column=0, sticky="ew", padx=16, pady=(2, 10))
            self.geometry("560x720")
        else:
            self.btn_detail.configure(text="▸ 詳細設定（保存先・表紙などの変更）")
            self.frm_detail.grid_forget()
            self.geometry("560x420")

    def _toggle_log(self):
        self._log_open = not self._log_open
        if self._log_open:
            self.btn_log.configure(text="詳細を隠す")
            self.txt_log.grid(row=9, column=0, sticky="ew", padx=20, pady=(2, 10))
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("end", "\n".join(self._raw_log) + "\n")
            self.txt_log.see("end")
        else:
            self.btn_log.configure(text="詳細を表示")
            self.txt_log.grid_forget()

    def _on_cover_change(self):
        is_file = (self.var_cover.get() == "file")
        state = "normal" if is_file else "disabled"
        self.btn_cover_pick.configure(state=state)
        self.lbl_cover_file.configure(text_color=("gray10", "gray90") if is_file else "gray")
        self._persist()

    def _pick_output_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or default_output_dir())
        if d:
            self.var_outdir.set(d)
            self._persist()

    def _open_alphapolis_batch_dialog(self):
        if self._proc is not None:
            return
        saved = self.settings.get("alphapolis_batch", default_settings()["alphapolis_batch"])
        win = ctk.CTkToplevel(self)
        win.title("AlphaPolis 批量筛选下载")
        win.geometry("460x390")
        win.transient(self)
        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=22, pady=18)
        form.grid_columnconfigure(1, weight=1)

        category = ctk.StringVar(value=_CATEGORY_BY_ID.get(saved["category_id"], "恋爱"))
        complete = ctk.BooleanVar(value=bool(saved["complete_only"]))
        char_min = ctk.StringVar(value="" if saved["char_min"] is None else str(saved["char_min"]))
        char_max = ctk.StringVar(value="" if saved["char_max"] is None else str(saved["char_max"]))
        sort = ctk.StringVar(value=_SORT_BY_VALUE.get(saved["sort"], "收藏数"))
        target = ctk.StringVar(value=str(saved["target_count"]))

        ctk.CTkLabel(form, text="题材").grid(row=0, column=0, sticky="w", pady=6)
        ctk.CTkOptionMenu(form, values=list(ALPHAPOLIS_CATEGORIES), variable=category).grid(
            row=0, column=1, sticky="ew", pady=6)
        ctk.CTkCheckBox(form, text="只下载已完结", variable=complete).grid(
            row=1, column=1, sticky="w", pady=6)
        ctk.CTkLabel(form, text="最小字数").grid(row=2, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=char_min, placeholder_text="不限制").grid(
            row=2, column=1, sticky="ew", pady=6)
        ctk.CTkLabel(form, text="最大字数").grid(row=3, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=char_max, placeholder_text="不限制").grid(
            row=3, column=1, sticky="ew", pady=6)
        ctk.CTkLabel(form, text="排序").grid(row=4, column=0, sticky="w", pady=6)
        ctk.CTkOptionMenu(form, values=list(ALPHAPOLIS_SORTS), variable=sort).grid(
            row=4, column=1, sticky="ew", pady=6)
        ctk.CTkLabel(form, text="下载数量").grid(row=5, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=target).grid(row=5, column=1, sticky="ew", pady=6)

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(20, 0))

        def start():
            try:
                filters = _batch_filters_from_values(
                    category.get(), complete.get(), char_min.get(), char_max.get(), sort.get())
                target_count = _batch_target_from_text(target.get())
            except ValueError as exc:
                messagebox.showwarning("筛选条件有误", str(exc), parent=win)
                return
            settings = self._collect_settings()
            settings["alphapolis_batch"] = {
                "category_id": filters.category_id, "complete_only": filters.complete_only,
                "char_min": filters.char_min, "char_max": filters.char_max,
                "sort": filters.sort, "target_count": target_count,
            }
            self.settings = settings
            save_settings(settings)
            win.destroy()
            self._launch_alphapolis_batch(filters, target_count, settings)

        ctk.CTkButton(buttons, text="取消", fg_color="gray40", command=win.destroy).pack(side="right")
        ctk.CTkButton(buttons, text="开始批量下载", command=start).pack(side="right", padx=(0, 8))

    def _open_batch_dialog(self):
        if self._proc is not None:
            return
        win = ctk.CTkToplevel(self)
        win.title("複数リンク / TXTを読み込む")
        win.geometry("720x520")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text="リンク一覧をそのまま貼り付けるか、TXTファイルを読み込んでください。\n"
                 "タグ・分類URLは自動的に除外します。",
            justify="left", anchor="w").pack(fill="x", padx=16, pady=(14, 6))
        box = ctk.CTkTextbox(win)
        box.pack(fill="both", expand=True, padx=16, pady=6)

        def load_txt():
            path = filedialog.askopenfilename(
                parent=win, filetypes=[("テキストファイル", "*.txt"), ("すべて", "*.*")])
            if not path:
                return
            content = ""
            for enc in ("utf-8-sig", "utf-8", "cp932"):
                try:
                    with open(path, "r", encoding=enc) as f:
                        content = f.read()
                    break
                except UnicodeError:
                    continue
            box.delete("1.0", "end")
            box.insert("1.0", content)

        def start_batch():
            if self._proc is not None:
                return
            text = box.get("1.0", "end").strip()
            urls = self._extract_urls(text)
            if not urls:
                return
            win.destroy()
            self._persist()
            self._launch_inspection(text, self._collect_settings())

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(4, 14))
        ctk.CTkButton(buttons, text="TXTファイルを選ぶ", command=load_txt).pack(side="left")
        ctk.CTkButton(buttons, text="抽出して順番にダウンロード",
                      command=start_batch).pack(side="right")

    def _pick_cover_image(self):
        f = filedialog.askopenfilename(
            filetypes=[("画像ファイル", "*.jpg *.jpeg *.png"), ("すべて", "*.*")])
        if f:
            self._cover_image_path = f
            self.var_cover_file.set(os.path.basename(f))
            self._persist()

    def _pick_font(self):
        f = filedialog.askopenfilename(
            filetypes=[("フォントファイル", "*.otf *.ttf *.woff *.woff2"), ("すべて", "*.*")])
        if f:
            self._font_path = f
            self.var_font_name.set(os.path.basename(f))
            self._persist()

    def _clear_font(self):
        self._font_path = ""
        self.var_font_name.set("標準（埋め込みなし）")
        self._persist()

    # ── 起動時クリップボード自動入力（§7.2） ───────────────────
    def _start_clipboard_autofill(self):
        try:
            text = self.clipboard_get()       # clipboard_get は UIスレッドで
        except Exception:
            return
        text = (text or "").strip()
        if not (text.startswith("http://") or text.startswith("https://")):
            return
        threading.Thread(target=self._clipboard_worker, args=(text,),
                         daemon=True).start()

    def _clipboard_worker(self, url: str):
        info = detect_site(url)
        if info and info.get("site") and not info.get("needs_playwright"):
            self._queue.put(("autofill", url))

    # ── 終了 ─────────────────────────────────────────────────
    def _on_close(self):
        self._persist()
        if hasattr(self._proc, "terminate"):
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.destroy()


def main():
    app = NovelDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
