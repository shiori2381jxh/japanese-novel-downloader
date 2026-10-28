import threading
from pathlib import Path
from types import SimpleNamespace

import novel_downloader_gui as gui
from alphapolis_search import SearchFilters


settings = gui.load_settings()
batch = settings["alphapolis_batch"]
filters = SearchFilters(
    str(batch["category_id"]), bool(batch["complete_only"]),
    batch["char_min"], batch["char_max"], str(batch["sort"]),
)
log_path = Path(settings["output_dir"]) / "alphapolis_batch_resume.log"
log_path.parent.mkdir(parents=True, exist_ok=True)


class LoggedQueue:
    def put(self, message):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(repr(message) + "\n")


app = SimpleNamespace(_abort_event=threading.Event(), _queue=LoggedQueue(), _proc=None)
app._inspect_url_candidate = lambda url, cfg: gui.NovelDownloaderApp._inspect_url_candidate(app, url, cfg)
app._download_candidate = lambda candidate, cfg: gui.NovelDownloaderApp._download_candidate(app, candidate, cfg)
app._build_cli_args = lambda url, cfg: gui.NovelDownloaderApp._build_cli_args(app, url, cfg)

with log_path.open("a", encoding="utf-8") as handle:
    handle.write("=== AlphaPolis resume started ===\n")
gui.NovelDownloaderApp._alphapolis_batch_worker(app, filters, int(batch["target_count"]), settings)
with log_path.open("a", encoding="utf-8") as handle:
    handle.write("=== AlphaPolis resume ended ===\n")
