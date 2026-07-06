#!/usr/bin/env python3
"""
Walkyrie v1: Wallhaven Wallpaper Downloader
============================================
An interactive command-line tool to search and download wallpapers from
Wallhaven (https://wallhaven.cc) using their public v1 API.

Features:
  - Search by keyword/tag, category, purity, orientation
  - Multiple sort modes (Latest, Toplist, Random, Views, Favorites, Relevance)
  - Optional API key for NSFW content & personal settings
  - Download N wallpapers or all matching results
  - Custom output folder (defaults to ./<search_phrase>/)
  - Pause / resume / abort controls while downloading

Config & Queue:
  - Settings live at : ~/.walkyrie/config.json   (API key, worker count)
  - Queue/history at : ~/.walkyrie/walkyrie.db   (SQLite database)
  - While downloading, type 'p' + Enter to pause, 'r' + Enter to resume,
    or 'q' + Enter to stop early (progress is kept, the rest stays queued).
  - While answering wizard questions, type 'b' + Enter to go back to the
    main menu at any point.

Requirements:
  pip install requests rich

Usage:
  python Walkyrie.py                       # interactive wizard
  python Walkyrie.py --query "cyberpunk" --amount 20 --category anime
  python Walkyrie.py --resume              # just process the saved queue
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Iterator

import requests
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.progress import (
    Progress, BarColumn, TextColumn, TimeRemainingColumn,
    SpinnerColumn, MofNCompleteColumn,
)

API_BASE = "https://wallhaven.cc/api/v1"
VERSION = "1.0.0"

OLD_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".wallhaven_downloader")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".walkyrie")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.path.join(CONFIG_DIR, "walkyrie.db")

MAX_WORKERS_DEFAULT = 4
RATE_LIMIT_PER_MIN = 45
MIN_REQUEST_INTERVAL = 60.0 / RATE_LIMIT_PER_MIN  # ~1.33s between requests

console = Console()


# ----------------------------------------------------------------------
# Navigation control: raised anywhere inside the wizard to bail back to
# the main menu without losing the process (queue/config stay intact).
# ----------------------------------------------------------------------
class BackToMenu(Exception):
    """Raised when the user wants to abandon the current flow and return
    to the main menu."""


# ----------------------------------------------------------------------
# Console output helpers
# ----------------------------------------------------------------------
def banner():
    text = Text(justify="center")
    text.append("✨️  ", style="bold magenta")
    text.append("WALKYRIE", style="bold yellow")
    text.append("  ✨\n", style="bold magenta")
    text.append("⚔️  ", style="bold magenta")
    text.append("Fetching Valhalla to your Desktop", style="italic cyan")
    text.append("  ⚔️\n", style="bold magenta")
    text.append("⚡ ", style="bold magenta")
    text.append("Wallpaper Downloader", style="bold white")
    text.append(" ⚡", style="bold magenta")

    panel = Panel(
        Align.center(text, vertical="middle"),
        title="[bold cyan]𖤍[/]",
        border_style="bright_blue",
        subtitle=f"[dim]v{VERSION}[/]",
        width=60,
        padding=(1, 2),
    )
    console.print(panel)
    console.print()


def info(msg: str):
    console.print(f"[cyan]ℹ️  {msg}[/cyan]")


def success(msg: str):
    console.print(f"[green]✅ {msg}[/green]")


def warn(msg: str):
    console.print(f"[yellow]⚠️  {msg}[/yellow]")


def error(msg: str):
    console.print(f"[bold red]❌ {msg}[/bold red]")


def divider():
    console.rule(style="dim")


# ----------------------------------------------------------------------
# Rate limiter — shared across threads so it never blow the 45/min budget
# ----------------------------------------------------------------------
class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)


def api_get(url: str, params: Optional[dict] = None, api_key: Optional[str] = None,
            stream: bool = False, timeout: int = 20, max_retries: int = 5):
    """A GET wrapper with rate limiting, retry/backoff on 429s and transient
    network errors, and header-based authentication."""
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    backoff = 2.0
    resp = None
    for attempt in range(1, max_retries + 1):
        rate_limiter.wait()
        try:
            resp = requests.get(url, params=params, headers=headers,
                                 stream=stream, timeout=timeout)
        except requests.RequestException as e:
            if attempt == max_retries:
                raise
            warn(f"Network hiccup ({e}); retrying in {backoff:.0f}s "
                 f"[{attempt}/{max_retries}]...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        if resp.status_code == 429:
            if attempt == max_retries:
                return resp
            warn(f"Rate limited (429). Waiting {backoff:.0f}s before retry "
                 f"[{attempt}/{max_retries}]...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        return resp

    return resp  # pragma: no cover


# ----------------------------------------------------------------------
# Settings file (API key + preferences only)
# ----------------------------------------------------------------------
def default_settings() -> dict:
    return {
        "version": VERSION,
        "api_key": None,
        "max_workers": MAX_WORKERS_DEFAULT,
    }


def load_settings() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return default_settings()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base = default_settings()
        base.update({k: v for k, v in cfg.items() if k in base})
        return base, cfg  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        warn("Settings file was unreadable/corrupted. Starting fresh.")
        return default_settings(), {}


def save_settings(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        error(f"Could not save settings file: {e}")


_settings_lock = threading.Lock()


def save_settings_locked(cfg: dict):
    with _settings_lock:
        save_settings(cfg)


# ----------------------------------------------------------------------
# One-time migration: old ~/.wallhaven_downloader dir & legacy JSON queue
# ----------------------------------------------------------------------
def migrate_legacy_directory():
    """If the old config directory exists and the new one doesn't, move it
    wholesale so nothing is lost (API key, and the legacy queue JSON if
    present will be picked up by migrate_legacy_queue() afterwards)."""
    if os.path.isdir(OLD_CONFIG_DIR) and not os.path.isdir(CONFIG_DIR):
        try:
            shutil.move(OLD_CONFIG_DIR, CONFIG_DIR)
            info(f"📦 Migrated old config directory to {CONFIG_DIR}")
        except OSError as e:
            warn(f"Could not migrate old config directory automatically: {e}")


def migrate_legacy_queue(raw_cfg: dict, db: "QueueDB"):
    """Older versions of Walkyrie stored the queue and downloaded-id history
    as JSON arrays inside config.json. At a few thousand items that file
    becomes huge and slow to rewrite atomically on every download. Move
    any such legacy data into the SQLite database once, then strip it from
    the JSON file."""
    legacy_queue = raw_cfg.pop("queue", None)
    legacy_downloaded = raw_cfg.pop("downloaded_ids", None)

    if legacy_queue:
        info(f"📦 Migrating {len(legacy_queue)} legacy queue item(s) into the database...")
        rows = []
        for item in legacy_queue:
            if not item.get("id") or not item.get("folder"):
                continue
            rows.append((item["id"], item.get("path"), item.get("file_size"), item["folder"]))
        db.add_many(rows)

    if legacy_downloaded:
        info(f"📦 Migrating {len(legacy_downloaded)} legacy downloaded-id record(s)...")
        db.mark_many_downloaded(legacy_downloaded)

    if legacy_queue or legacy_downloaded:
        settings = default_settings()
        settings.update({k: v for k, v in raw_cfg.items() if k in settings})
        save_settings(settings)
        success("Legacy queue data migrated to walkyrie.db 🎉")


# ----------------------------------------------------------------------
# SQLite-backed queue + download history
# ----------------------------------------------------------------------
class QueueDB:
    """Thread-safe wrapper around the SQLite queue/history database.

    Using SQLite instead of a JSON array means adding/removing single
    items is an indexed O(log n) operation instead of rewriting the whole
    file, which matters a lot once the queue holds thousands of entries.
    """

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id         TEXT NOT NULL,
                    path       TEXT,
                    file_size  INTEGER,
                    folder     TEXT NOT NULL,
                    added_at   TEXT NOT NULL,
                    PRIMARY KEY (id, folder)
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_id ON queue(id)")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS downloaded (
                    id             TEXT PRIMARY KEY,
                    downloaded_at  TEXT NOT NULL
                )
            """)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- downloaded history -------------------------------------------------
    def is_downloaded(self, wallpaper_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM downloaded WHERE id = ?", (wallpaper_id,))
            return cur.fetchone() is not None

    def downloaded_ids(self) -> set:
        with self._lock:
            cur = self._conn.execute("SELECT id FROM downloaded")
            return {row[0] for row in cur.fetchall()}

    def mark_downloaded(self, wallpaper_id: str):
        if not wallpaper_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO downloaded (id, downloaded_at) VALUES (?, ?)",
                (wallpaper_id, self._now()),
            )

    def mark_many_downloaded(self, ids: List[str]):
        now = self._now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO downloaded (id, downloaded_at) VALUES (?, ?)",
                [(i, now) for i in ids if i],
            )

    def downloaded_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM downloaded")
            return cur.fetchone()[0]

    # -- queue ---------------------------------------------------------------
    def add_many(self, rows: List[tuple]) -> int:
        """rows: list of (id, path, file_size, folder). Returns count of
        genuinely new rows inserted (ignores duplicates and anything
        already marked as downloaded)."""
        if not rows:
            return 0
        now = self._now()
        with self._lock, self._conn:
            before = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO queue (id, path, file_size, folder, added_at)
                SELECT ?, ?, ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM downloaded WHERE downloaded.id = ?)
                """,
                [(r[0], r[1], r[2], r[3], now, r[0]) for r in rows],
            )
            after = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        return after - before

    def get_all(self) -> List[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, path, file_size, folder FROM queue ORDER BY added_at ASC"
            )
            return [
                {"id": r[0], "path": r[1], "file_size": r[2], "folder": r[3]}
                for r in cur.fetchall()
            ]

    def remove(self, wallpaper_id: str, folder: str):
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM queue WHERE id = ? AND folder = ?", (wallpaper_id, folder)
            )

    def remove_already_downloaded(self):
        """Drop any queue rows whose id has since been marked downloaded
        (e.g. downloaded from a previous run/folder)."""
        with self._lock, self._conn:
            self._conn.execute("""
                DELETE FROM queue
                WHERE id IN (SELECT id FROM downloaded)
            """)

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM queue")
            return cur.fetchone()[0]

    def clear(self):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM queue")

    def close(self):
        with self._lock:
            self._conn.close()


# ----------------------------------------------------------------------
# Pause / Resume / Abort controller for downloads
# ----------------------------------------------------------------------
class DownloadController:
    """
    Background thread listens on stdin for commands while a download is in
    progress, so the user can pause/resume/abort without killing the script.

      p -> pause (workers finish their current file, then wait)
      r -> resume a paused download
      q -> abort (progress already saved is kept; queue keeps the rest)
    """

    def __init__(self):
        self.paused = threading.Event()
        self.abort = threading.Event()
        self._thread = None

    def start(self):
        self.paused.clear()
        self.abort.clear()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        console.print(
            "[bold magenta]⌨️  Controls:[/bold magenta] type 'p' + Enter to pause, "
            "'r' + Enter to resume, 'q' + Enter to stop and save progress."
        )

    def _listen(self):
        while not self.abort.is_set():
            try:
                line = sys.stdin.readline()
            except (EOFError, ValueError):
                return
            if not line:
                return
            cmd = line.strip().lower()
            if cmd == "p":
                if not self.paused.is_set():
                    self.paused.set()
                    warn("⏸️  Pausing after in-flight files finish... (type 'r' to resume)")
            elif cmd == "r":
                if self.paused.is_set():
                    self.paused.clear()
                    success("▶️  Resuming download...")
            elif cmd == "q":
                self.abort.set()
                self.paused.clear()
                warn("🛑 Stopping... progress is saved, remaining items stay in the queue.")
                return

    def wait_if_paused(self):
        while self.paused.is_set() and not self.abort.is_set():
            time.sleep(0.3)


# ----------------------------------------------------------------------
# Menu / prompt utilities
# ----------------------------------------------------------------------
def choose_from_menu(title: str, options: list, default_index: int = 0, allow_back: bool = False):
    """Renders a numbered menu with rich. If allow_back is True, option '0'
    (or typing 'b'/'back') raises BackToMenu instead of returning a value."""
    table = Table(title=f"🔹 {title}", show_header=False, box=None, padding=(0, 1))
    table.add_column(justify="right", style="cyan", no_wrap=True)
    table.add_column()
    for i, (label, _value) in enumerate(options, start=1):
        marker = " [dim](default)[/dim]" if (i - 1) == default_index else ""
        table.add_row(f"{i}.", f"{label}{marker}")
    if allow_back:
        table.add_row("0.", "[dim]⬅️  Back to Main Menu[/dim]")
    console.print(table)

    choice = Prompt.ask(
        f"Select an option [1-{len(options)}]" + (" (or 0/b to go back)" if allow_back else ""),
        default=str(default_index + 1),
    ).strip().lower()

    if allow_back and choice in ("0", "b", "back"):
        raise BackToMenu()
    if choice == "":
        return options[default_index][1]
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1][1]
    warn("Invalid choice, using default.")
    return options[default_index][1]


def ask_text(msg: str, default: str = "", allow_back: bool = True) -> str:
    """A free-text prompt. If allow_back is True, typing 'b'/'back' raises
    BackToMenu instead of returning the literal text."""
    hint = f"{msg} [dim](type 'b' to go back)[/dim]" if allow_back else msg
    val = Prompt.ask(hint, default=default)
    if allow_back and val.strip().lower() in ("b", "back"):
        raise BackToMenu()
    return val


def confirm_or_back(question: str, default_yes: bool = False, allow_back: bool = True) -> bool:
    """A yes/no confirmation that also accepts 'b'/'back' to bail out to
    the main menu when allow_back is True."""
    if not allow_back:
        return Confirm.ask(question, default=default_yes)

    default_str = "y" if default_yes else "n"
    while True:
        ans = Prompt.ask(f"{question} [y/n/b]", default=default_str).strip().lower()
        if ans in ("b", "back"):
            raise BackToMenu()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        if ans == "":
            return default_yes
        warn("Please answer y, n, or b (back).")


def yes_no(question: str, default_yes: bool = False) -> bool:
    """Plain confirmation with no back option — used at the top-level main
    menu where "back" wouldn't mean anything."""
    return Confirm.ask(question, default=default_yes)


# ----------------------------------------------------------------------
# Building the API query
# ----------------------------------------------------------------------
def build_categories_bits(choice):
    mapping = {"general": "100", "anime": "010", "people": "001", "all": "111"}
    return mapping[choice]


def build_purity_bits(choice):
    mapping = {
        "sfw": "100", "sketchy": "010", "nsfw": "001",
        "sfw+sketchy": "110", "all": "111",
    }
    return mapping[choice]


def sanitize_filename(name):
    name = (name or "").strip() or "wallhaven_downloads"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:80]


@dataclass
class SearchParams:
    api_key: Optional[str] = None
    query: str = ""
    exclude_tags: List[str] = field(default_factory=list)
    category: str = "all"
    purity: str = "sfw"
    orientation: str = "any"
    sorting: str = "date_added"
    top_range: Optional[str] = None
    resolutions: Optional[str] = None
    ratios: Optional[str] = None
    colors: Optional[str] = None
    seed: Optional[str] = None
    amount: Optional[int] = None
    folder: str = "wallhaven_downloads"
    workers: int = MAX_WORKERS_DEFAULT


def collect_search_params(cfg: dict) -> SearchParams:
    """Runs the interactive wizard. Any step can raise BackToMenu (via
    ask_text / choose_from_menu / confirm_or_back with allow_back=True),
    which propagates straight up to the caller so the user lands back on
    the main menu with nothing half-applied."""
    api_key = cfg.get("api_key")
    if api_key:
        info(f"🔑 Using saved API key ({api_key[:4]}{'*' * max(0, len(api_key) - 4)})")
        if confirm_or_back("Change/remove the saved API key?", default_yes=False):
            new_key = ask_text("🔑 Enter new API key (leave blank to remove it)")
            api_key = new_key if new_key else None
            cfg["api_key"] = api_key
            save_settings_locked(cfg)
    else:
        entered = ask_text("🔑 Enter your Wallhaven API key (leave blank to skip / SFW only)")
        api_key = entered if entered else None
        if api_key and confirm_or_back("Save this API key for next time?", default_yes=True):
            cfg["api_key"] = api_key
            save_settings_locked(cfg)

    query = ask_text("🔍 Enter a search phrase / tag (leave blank for latest wallpapers)")

    exclude_raw = ask_text("🚫 Tags to exclude, comma separated (optional)")
    exclude_tags = [t.strip() for t in exclude_raw.split(",") if t.strip()]

    category = choose_from_menu(
        "Category",
        [("General", "general"), ("Anime", "anime"), ("People", "people"),
         ("All categories", "all")],
        default_index=3,
        allow_back=True,
    )

    purity_options = [
        ("SFW", "sfw"),
        ("Sketchy", "sketchy"),
        ("NSFW (requires API key)", "nsfw"),
        ("SFW + Sketchy", "sfw+sketchy"),
        ("All (requires API key for full NSFW)", "all"),
    ]
    purity = choose_from_menu("Purity filter", purity_options, default_index=0, allow_back=True)
    if purity in ("nsfw", "all") and not api_key:
        warn("NSFW content requires an API key. Falling back to SFW + Sketchy.")
        purity = "sfw+sketchy" if purity == "all" else "sfw"

    orientation = choose_from_menu(
        "Orientation",
        [("Any", "any"), ("Wide / Landscape", "wide"), ("Portrait / Tall", "portrait")],
        default_index=0,
        allow_back=True,
    )

    sort_options = [
        ("Latest (date_added)", "date_added"),
        ("Toplist (top wallpapers)", "toplist"),
        ("Random", "random"),
        ("Views (hot)", "views"),
        ("Favorites", "favorites"),
        ("Relevance", "relevance"),
    ]
    sorting = choose_from_menu("List / sort by", sort_options, default_index=0, allow_back=True)

    top_range = None
    if sorting == "toplist":
        range_options = [
            ("Last day", "1d"), ("Last 3 days", "3d"), ("Last week", "1w"),
            ("Last month", "1M"), ("Last 3 months", "3M"),
            ("Last 6 months", "6M"), ("Last year", "1y"),
        ]
        top_range = choose_from_menu("Toplist range", range_options, default_index=3, allow_back=True)

    seed = None
    if sorting == "random":
        seed_raw = ask_text("🌱 Optional random seed (blank = new random each search)")
        seed = seed_raw or None

    resolutions = ask_text("📐 Exact resolution(s), e.g. 1920x1080,2560x1440 (optional)") or None
    ratios = ask_text("📏 Aspect ratio(s), e.g. 16x9,16x10 (optional)") or None

    advanced_color = confirm_or_back("Filter by a specific hex color?", default_yes=False)
    colors = None
    if advanced_color:
        colors = ask_text("🎨 Hex color (no #), e.g. 336600") or None

    amount_raw = ask_text("📦 How many wallpapers to download? (number, or 'all')").lower()
    if amount_raw in ("", "all"):
        amount = None
    else:
        try:
            amount = max(1, int(amount_raw))
        except ValueError:
            warn("Invalid number, defaulting to 24.")
            amount = 24

    default_folder = os.path.join(
        os.getcwd(), sanitize_filename(query) if query else "wallhaven_downloads"
    )
    folder = ask_text(f"📁 Output folder (Enter for default: {default_folder})", default="") or default_folder

    workers_raw = ask_text(f"🧵 Concurrent downloads (Enter for {cfg.get('max_workers', MAX_WORKERS_DEFAULT)})")
    try:
        workers = int(workers_raw) if workers_raw else cfg.get("max_workers", MAX_WORKERS_DEFAULT)
        workers = max(1, min(workers, 10))
    except ValueError:
        workers = cfg.get("max_workers", MAX_WORKERS_DEFAULT)

    return SearchParams(
        api_key=api_key, query=query, exclude_tags=exclude_tags, category=category,
        purity=purity, orientation=orientation, sorting=sorting, top_range=top_range,
        resolutions=resolutions, ratios=ratios, colors=colors, seed=seed,
        amount=amount, folder=folder, workers=workers,
    )


# ----------------------------------------------------------------------
# Orientation filter (client-side, since ratio param needs exact values)
# ----------------------------------------------------------------------
def matches_orientation(item: dict, orientation: str) -> bool:
    if orientation == "any":
        return True
    try:
        w = int(item["dimension_x"])
        h = int(item["dimension_y"])
    except (KeyError, ValueError, TypeError):
        return True
    if orientation == "wide":
        return w >= h
    if orientation == "portrait":
        return h > w
    return True


# ----------------------------------------------------------------------
# API interaction
# ----------------------------------------------------------------------
def build_query_string(params: SearchParams) -> str:
    q = params.query or ""
    for tag in params.exclude_tags:
        q = (q + f" -{tag}").strip()
    return q


def search_wallpapers(params: SearchParams) -> Iterator[dict]:
    """Generator that yields wallpaper metadata dicts matching the filters,
    paging through the API as needed."""
    api_params: Dict[str, Any] = {
        "q": build_query_string(params),
        "categories": build_categories_bits(params.category),
        "purity": build_purity_bits(params.purity),
        "sorting": params.sorting,
        "order": "desc",
    }
    if params.sorting == "toplist" and params.top_range:
        api_params["topRange"] = params.top_range
    if params.sorting == "random" and params.seed:
        api_params["seed"] = params.seed
    if params.resolutions:
        api_params["resolutions"] = params.resolutions
    if params.ratios:
        api_params["ratios"] = params.ratios
    if params.colors:
        api_params["colors"] = params.colors

    page = 1
    total_yielded = 0
    target = params.amount

    while True:
        api_params["page"] = page
        resp = api_get(f"{API_BASE}/search", params=api_params, api_key=params.api_key)

        if resp is None:
            error("Search request failed after retries.")
            return
        if resp.status_code == 401:
            error("Unauthorized (401). Check your API key.")
            return
        if resp.status_code != 200:
            error(f"Search request failed with status {resp.status_code}.")
            return

        data = resp.json()
        results = data.get("data", [])
        meta = data.get("meta", {})
        last_page = meta.get("last_page", page)

        if not results:
            break

        for item in results:
            if matches_orientation(item, params.orientation):
                yield item
                total_yielded += 1
                if target is not None and total_yielded >= target:
                    return

        if page >= last_page:
            break
        page += 1


# ----------------------------------------------------------------------
# Downloading (concurrent, with size verification)
# ----------------------------------------------------------------------
def download_image(item: dict, folder: str, api_key: Optional[str]) -> tuple:
    """Returns (ok: bool, filename: str, message: str)."""
    url = item.get("path")
    if not url:
        return False, "(unknown)", "No direct path found"

    filename = os.path.basename(url)
    dest_path = os.path.join(folder, filename)
    expected_size = item.get("file_size")

    if os.path.exists(dest_path):
        if not expected_size or os.path.getsize(dest_path) == expected_size:
            return True, filename, "already exists"
        # size mismatch -> re-download over it
        warn(f"Existing file size mismatch for {filename}, re-downloading.")

    tmp_path = dest_path + ".part"
    try:
        resp = api_get(url, api_key=api_key, stream=True, timeout=30)
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "?"
            return False, filename, f"HTTP {code}"

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)

        if expected_size and os.path.getsize(tmp_path) != expected_size:
            os.remove(tmp_path)
            return False, filename, "size verification failed"

        os.replace(tmp_path, dest_path)
        return True, filename, "downloaded"
    except requests.RequestException as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False, filename, str(e)
    except OSError as e:
        return False, filename, f"disk error: {e}"


def add_to_queue(db: QueueDB, wallpapers: List[dict], folder: str) -> int:
    rows = [(w.get("id"), w.get("path"), w.get("file_size"), folder) for w in wallpapers if w.get("id")]
    before_downloaded = db.downloaded_ids()
    skipped = sum(1 for w in wallpapers if w.get("id") in before_downloaded)
    added = db.add_many(rows)
    if skipped:
        info(f"🟡 Skipped {skipped} wallpaper(s) already downloaded previously (in any folder).")
    return added


def process_queue(db: QueueDB, api_key: Optional[str], workers: Optional[int] = None,
                   default_workers: int = MAX_WORKERS_DEFAULT):
    """Downloads everything currently in the queue concurrently, saving
    progress to the database as it goes so the queue can always be safely
    resumed later even if the process is killed mid-download."""
    db.remove_already_downloaded()
    items_to_process = db.get_all()
    if not items_to_process:
        warn("The queue is empty. Nothing to download.")
        return

    workers = workers or default_workers
    total = len(items_to_process)
    success(f"📥 Processing queue: {total} item(s) pending — {workers} worker(s), "
            f"respecting the {RATE_LIMIT_PER_MIN}/min API limit 🚀")
    divider()

    for item in items_to_process:
        os.makedirs(item["folder"], exist_ok=True)

    controller = DownloadController()
    controller.start()

    downloaded = 0
    failed = 0
    lock = threading.Lock()

    progress_columns = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    ]

    def handle_result(item, ok, filename, message):
        nonlocal downloaded, failed
        with lock:
            if ok:
                downloaded += 1
                db.mark_downloaded(item.get("id"))
                db.remove(item["id"], item["folder"])
                tag = "🟡 skip" if message == "already exists" else "🖼️  ok"
                success(f"[{downloaded + failed}/{total}] {tag}: {filename}")
            else:
                failed += 1
                warn(f"[{downloaded + failed}/{total}] ❌ kept in queue "
                     f"({message}): {filename}")

    try:
        with Progress(*progress_columns, console=console, transient=False) as bar:
            task_id = bar.add_task("Downloading wallpapers", total=total)

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_item = {}
                for item in items_to_process:
                    if controller.abort.is_set():
                        break
                    controller.wait_if_paused()
                    if controller.abort.is_set():
                        break
                    fut = pool.submit(download_image, item, item["folder"], api_key)
                    future_to_item[fut] = item

                for fut in concurrent.futures.as_completed(future_to_item):
                    item = future_to_item[fut]
                    try:
                        ok, filename, message = fut.result()
                    except Exception as e:  # noqa: BLE001
                        ok, filename, message = False, item.get("id", "?"), str(e)
                    handle_result(item, ok, filename, message)
                    bar.update(task_id, advance=1)
    except (KeyboardInterrupt, EOFError):
        print()
        warn("Download interrupted. Progress saved — remaining items stay in the queue.")
        controller.abort.set()
        sys.exit(1)

    controller.abort.set()

    divider()
    remaining = db.count()
    success(f"🎉 Done! {downloaded}/{total} wallpapers downloaded this session.")
    if failed:
        warn(f"{failed} item(s) failed and remain queued for a future retry.")
    if remaining:
        warn(f"{remaining} item(s) remain in the queue — resume anytime from the main menu.")


def run_new_search(cfg: dict, db: QueueDB, params: Optional[SearchParams] = None,
                    auto_download: Optional[bool] = None):
    """Collects search parameters (or uses ones already given via CLI),
    runs the search, and enqueues results. If the user backs out of the
    wizard, this simply returns without side effects."""
    if params is None:
        try:
            params = collect_search_params(cfg)
        except BackToMenu:
            info("↩️  Returned to the main menu — nothing was searched or queued.")
            return

    divider()
    info("Search settings:")
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="white")
    table.add_column(style="bold white")
    table.add_row("🔍 Query", params.query or "(none - latest)")
    if params.exclude_tags:
        table.add_row("🚫 Excluding", ", ".join(params.exclude_tags))
    table.add_row("🗂️  Category", params.category)
    table.add_row("🔞 Purity", params.purity)
    table.add_row("📐 Orientation", params.orientation)
    table.add_row("🔃 Sort by", params.sorting + (f" ({params.top_range})" if params.top_range else ""))
    if params.resolutions:
        table.add_row("📏 Resolutions", params.resolutions)
    if params.ratios:
        table.add_row("📐 Ratios", params.ratios)
    if params.colors:
        table.add_row("🎨 Color", params.colors)
    table.add_row("📦 Amount", "ALL" if params.amount is None else str(params.amount))
    table.add_row("📁 Folder", params.folder)
    table.add_row("🧵 Workers", str(params.workers))
    console.print(table)
    divider()

    if auto_download is None:
        if not yes_no("Search Wallhaven and add matching wallpapers to the queue?", default_yes=True):
            warn("Search cancelled — nothing was added to the queue.")
            return

    os.makedirs(params.folder, exist_ok=True)

    info("🔎 Searching Wallhaven, please wait...")
    wallpapers = list(search_wallpapers(params))

    if not wallpapers:
        warn("No wallpapers found matching your criteria.")
        return

    added = add_to_queue(db, wallpapers, params.folder)
    success(f"Found {len(wallpapers)} wallpaper(s). Added {added} new item(s) to the queue 📥")
    info(f"Queue now has {db.count()} item(s) total waiting to download.")

    if auto_download is None:
        auto_download = yes_no("Start downloading the queue now? (No = keep it saved for later)",
                                default_yes=True)
    if auto_download:
        process_queue(db, cfg.get("api_key"), workers=params.workers,
                      default_workers=cfg.get("max_workers", MAX_WORKERS_DEFAULT))
    else:
        info("👍 Saved. Pick 'Resume pending queue' from the main menu whenever you're ready.")


# ----------------------------------------------------------------------
# Interactive main menu
# ----------------------------------------------------------------------
def show_main_menu(cfg: dict, db: QueueDB):
    banner()
    queue_count = db.count()
    if cfg.get("api_key"):
        info("🔑 API key: saved")
    else:
        info("🔑 API key: none saved (SFW only)")
    info(f"🧵 Default concurrent downloads: {cfg.get('max_workers', MAX_WORKERS_DEFAULT)}")
    info(f"📚 Total wallpapers downloaded so far: {db.downloaded_count()}")

    options = []
    if queue_count:
        options.append((f"🪽 Resume Pending Queue ({queue_count} items)", "resume"))
    options.append(("🔮 Start A New Search", "new_search"))
    options.append(("🧵 Change Concurrent Download Count", "set_workers"))
    if queue_count:
        options.append(("🧹 Clear the Queue", "clear_queue"))
    options.append(("🌑 Exit to Midgard", "exit"))

    # allow_back is False here: this *is* the main menu already.
    return choose_from_menu("What would you like to do?", options, default_index=0, allow_back=False)


def interactive_main():
    cfg, db = startup()
    try:
        while True:
            choice = show_main_menu(cfg, db)

            try:
                if choice == "resume":
                    process_queue(db, cfg.get("api_key"),
                                  default_workers=cfg.get("max_workers", MAX_WORKERS_DEFAULT))
                elif choice == "new_search":
                    run_new_search(cfg, db)
                elif choice == "set_workers":
                    raw = ask_text("🧵 New default worker count (1-10)")
                    try:
                        n = max(1, min(10, int(raw)))
                        cfg["max_workers"] = n
                        save_settings_locked(cfg)
                        success(f"Default concurrent downloads set to {n}.")
                    except ValueError:
                        warn("Invalid number, no change made.")
                elif choice == "clear_queue":
                    if yes_no("Are you sure you want to clear the entire queue?", default_yes=False):
                        db.clear()
                        success("Queue cleared.")
                elif choice == "exit":
                    info("👋 Goodbye!")
                    break
            except BackToMenu:
                info("↩️  Returned to the main menu.")

            divider()
            if not yes_no("Return to the main menu?", default_yes=True):
                info("👋 Goodbye!")
                break
    except (KeyboardInterrupt, EOFError):
        print()
        warn("Interrupted. Your settings and queue have been saved.")
        sys.exit(1)
    finally:
        db.close()


# ----------------------------------------------------------------------
# Startup: migrations + settings + database
# ----------------------------------------------------------------------
def startup() -> tuple:
    migrate_legacy_directory()
    db = QueueDB(DB_PATH)

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            warn("Settings file was unreadable/corrupted. Starting fresh.")
            raw_cfg = {}
    else:
        raw_cfg = {}

    if "queue" in raw_cfg or "downloaded_ids" in raw_cfg:
        migrate_legacy_queue(raw_cfg, db)

    cfg = default_settings()
    cfg.update({k: v for k, v in raw_cfg.items() if k in cfg})
    return cfg, db


# ----------------------------------------------------------------------
# Non-interactive CLI mode
# ----------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="Walkyrie",
        description="Search and download wallpapers from Wallhaven.",
    )
    p.add_argument("--query", "-q", help="Search phrase / tag")
    p.add_argument("--exclude", help="Comma-separated tags to exclude")
    p.add_argument("--category", choices=["general", "anime", "people", "all"], default="all")
    p.add_argument("--purity", choices=["sfw", "sketchy", "nsfw", "sfw+sketchy", "all"], default="sfw")
    p.add_argument("--orientation", choices=["any", "wide", "portrait"], default="any")
    p.add_argument("--sorting", choices=["date_added", "toplist", "random", "views",
                                          "favorites", "relevance"], default="date_added")
    p.add_argument("--top-range", choices=["1d", "3d", "1w", "1M", "3M", "6M", "1y"])
    p.add_argument("--seed", help="Seed for random sorting")
    p.add_argument("--resolutions", help="Exact resolutions, e.g. 1920x1080,2560x1440")
    p.add_argument("--ratios", help="Aspect ratios, e.g. 16x9,16x10")
    p.add_argument("--colors", help="Hex color without #, e.g. 336600")
    p.add_argument("--amount", "-n", type=int, help="Number of wallpapers to download (omit = all)")
    p.add_argument("--folder", "-o", help="Output folder")
    p.add_argument("--api-key", help="Wallhaven API key (overrides saved key for this run)")
    p.add_argument("--workers", "-w", type=int, help="Concurrent download workers (1-10)")
    p.add_argument("--no-download", action="store_true",
                    help="Only search and enqueue; don't download yet")
    p.add_argument("--resume", action="store_true",
                    help="Skip search; just process whatever is already queued")
    p.add_argument("--clear-queue", action="store_true", help="Clear the queue and exit")
    return p


def cli_main(args: argparse.Namespace):
    cfg, db = startup()
    try:
        if args.clear_queue:
            db.clear()
            success("Queue cleared.")
            return

        api_key = args.api_key or cfg.get("api_key")

        if args.resume:
            process_queue(db, api_key, workers=args.workers,
                          default_workers=cfg.get("max_workers", MAX_WORKERS_DEFAULT))
            return

        purity = args.purity
        if purity in ("nsfw", "all") and not api_key:
            warn("NSFW content requires an API key. Falling back to SFW + Sketchy.")
            purity = "sfw+sketchy" if purity == "all" else "sfw"

        query = args.query or ""
        default_folder = os.path.join(
            os.getcwd(), sanitize_filename(query) if query else "wallhaven_downloads"
        )

        params = SearchParams(
            api_key=api_key,
            query=query,
            exclude_tags=[t.strip() for t in (args.exclude or "").split(",") if t.strip()],
            category=args.category,
            purity=purity,
            orientation=args.orientation,
            sorting=args.sorting,
            top_range=args.top_range,
            resolutions=args.resolutions,
            ratios=args.ratios,
            colors=args.colors,
            seed=args.seed,
            amount=args.amount,
            folder=args.folder or default_folder,
            workers=max(1, min(args.workers or cfg.get("max_workers", MAX_WORKERS_DEFAULT), 10)),
        )

        banner()
        run_new_search(cfg, db, params=params, auto_download=not args.no_download)
    finally:
        db.close()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # If no CLI search/queue flags were given at all, fall back to the
    # friendly interactive wizard (this preserves the original UX).
    no_flags_given = not any([
        args.query, args.exclude, args.amount, args.folder, args.api_key,
        args.workers, args.no_download, args.resume, args.clear_queue,
        args.top_range, args.seed, args.resolutions, args.ratios, args.colors,
    ]) and args.category == "all" and args.purity == "sfw" and args.orientation == "any" \
        and args.sorting == "date_added"

    try:
        if no_flags_given:
            interactive_main()
        else:
            cli_main(args)
    except (KeyboardInterrupt, EOFError):
        print()
        warn("Interrupted. Your settings and queue have been saved.")
        sys.exit(1)


if __name__ == "__main__":
    main()