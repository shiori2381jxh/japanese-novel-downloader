"""下载前书库判重所需的纯逻辑与安全持久化。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class LibraryDataError(RuntimeError):
    """书库文件无法安全读取或结构不合法。"""


@dataclass
class Candidate:
    """一次下载预检产生的候选作品。"""

    url: str
    normalized_url: str
    site: str
    title: str = ""
    library_name: str = ""
    status: str = ""
    selected: bool = False
    error: str = ""


def safe_library_name(title: str, fallback: str = "novel") -> str:
    """按下载引擎规则把作品标题转换成标准书库 TXT 文件名。"""

    name = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
    name = Path(name).name
    base = name[:60] if name else fallback
    return base + ".txt"


_TITLE_LINE = re.compile(r"^\s*タイトル\s*[：:]\s*(.+?)\s*$", re.MULTILINE)


def parse_dry_run_title(output: str) -> str:
    """从下载引擎的 dry-run 输出中提取作品标题。"""

    match = _TITLE_LINE.search(output)
    return match.group(1).strip() if match else ""


def load_library(path: Path) -> dict:
    """读取并验证书库；不存在时返回一个空书库。"""

    path = Path(path)
    if not path.exists():
        return {"recorded_names": [], "categories": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LibraryDataError(f"无法读取书库：{exc}") from exc
    if not isinstance(data, dict):
        raise LibraryDataError("书库顶层必须是对象")
    names = data.get("recorded_names")
    categories = data.get("categories")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise LibraryDataError("书库 recorded_names 必须是字符串列表")
    if not isinstance(categories, dict):
        raise LibraryDataError("书库 categories 必须是对象")
    return {"recorded_names": list(names), "categories": categories}


@contextmanager
def _exclusive_library_lock(path: Path):
    """跨进程锁住同一书库的完整读取、合并和替换事务。"""

    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_library_name(path: Path, name: str) -> None:
    """重新读取最新书库，合并一个书名并原子写回。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_library_lock(path):
        latest = load_library(path)
        latest["recorded_names"] = sorted(set(latest["recorded_names"]) | {name})
        file_descriptor, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(latest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def classify_candidates(
    candidates: list[Candidate], recorded_names: set[str]
) -> list[Candidate]:
    """按书库与本批输入顺序分类，并设置手动模式默认选择。"""

    seen: set[str] = set()
    for item in candidates:
        if item.error or not item.library_name:
            item.status, item.selected = "inspect_failed", False
        elif item.library_name in recorded_names:
            item.status, item.selected = "recorded", False
        elif item.library_name in seen:
            item.status, item.selected = "batch_duplicate", False
        else:
            item.status, item.selected = "new", True
        if item.library_name and item.status != "inspect_failed":
            seen.add(item.library_name)
    return candidates


def verify_txt_output(reported_path: str) -> bool:
    """验证引擎报告的普通 TXT 或分卷 TXT 目录确实存在。"""

    if not reported_path.strip():
        return False
    path = Path(reported_path.strip())
    if path.is_file():
        return path.suffix.lower() == ".txt"
    if path.is_dir():
        return any(
            child.is_file() and child.suffix.lower() == ".txt"
            for child in path.iterdir()
        )
    return False
