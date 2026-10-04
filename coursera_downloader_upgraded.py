#!/usr/bin/env python3
"""
coursera_downloader.py - robust downloader for Coursera courses you are
enrolled in (personal, offline use only).

Features
  * Cookie based auth (no password handling), full-URL or slug input
  * Automatic retries with exponential backoff + Retry-After support
  * Resumable downloads (HTTP Range) with atomic .part -> final rename
  * Size verification against Content-Length / Content-Range
  * Expired signed-link recovery (re-fetches the link once)
  * Thread pool with a global API rate limiter
  * Stable course-order numbering (module / lesson / lecture)
  * Multi-language subtitles (+ optional plain-text transcripts)
  * Module filter, list-only mode, free-disk-space warning
  * Clean Ctrl+C handling, log file, JSON report of results

Usage
  pip install requests
  python coursera_downloader.py <slug-or-course-url> --cookies cookies.txt

Requires Python 3.9+. Uses Coursera's internal web API, which may change.
Only download material you have legitimate access to, for personal use.
"""

from __future__ import annotations

import argparse
import copy
import http.cookiejar
import json
import logging
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "2.0.0"

BASE = "https://www.coursera.org"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK = 256 * 1024
MAX_DOWNLOAD_ATTEMPTS = 6
MIN_FREE_BYTES = 2 * 1024**3  # warn below 2 GB

log = logging.getLogger("coursera_dl")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class AuthError(Exception):
    """Cookies missing, expired or rejected."""


class CourseNotFound(Exception):
    """Slug is wrong or the account has no access to the course."""


class ExpiredLink(Exception):
    """A signed media/subtitle URL expired and must be re-fetched."""


class Cancelled(Exception):
    """User pressed Ctrl+C."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_RESERVED |= {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def clean(name: str, max_len: int = 90) -> str:
    """Return a string that is safe as a file or folder name on any OS."""
    name = _INVALID.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name[:max_len].rstrip(" .") or "untitled"


def extract_slug(value: str) -> str:
    m = re.search(r"/learn/([^/?#]+)", value)
    return m.group(1) if m else value.strip().strip("/")


def parse_ranges(spec: Optional[str]) -> Optional[set[int]]:
    """'1,3-5' -> {1,3,4,5}. None means no filter."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class RateLimiter:
    """Thread-safe minimum interval between API calls."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.min_interval
        if delay:
            time.sleep(delay)


# --------------------------------------------------------------------------- #
# HTTP sessions (one per thread, shared retry policy)
# --------------------------------------------------------------------------- #
class SessionFactory:
    def __init__(self, cookie_file: Path):
        jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise AuthError(f"Cookies file read nahi ho saki: {exc}") from exc
        if not any("coursera" in c.domain for c in jar):
            raise AuthError("cookies.txt mein coursera.org ki koi cookie nahi mili.")
        self._jar = jar
        self._local = threading.local()

    def get(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
            for c in self._jar:
                s.cookies.set_cookie(copy.copy(c))
            retry = Retry(
                total=5,
                connect=5,
                read=5,
                backoff_factor=1.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.session = s
        return s


@dataclass
class Context:
    sessions: SessionFactory
    limiter: RateLimiter
    stop: threading.Event = field(default_factory=threading.Event)


def api_json(ctx: Context, url: str) -> dict:
    if ctx.stop.is_set():
        raise Cancelled()
    ctx.limiter.wait()
    r = ctx.sessions.get().get(url, timeout=(10, 30))
    if r.status_code in (401, 403):
        raise AuthError(
            f"HTTP {r.status_code}: Coursera ne access reject kiya. "
            "Browser mein dobara login karke cookies.txt phir se export karein."
        )
    if r.status_code == 404:
        raise CourseNotFound(url)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as exc:
        raise AuthError("API ne JSON ki jagah HTML diya (shayad login expire ho gaya).") from exc


# --------------------------------------------------------------------------- #
# Course structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Lecture:
    module_idx: int
    module_name: str
    lesson_idx: int
    lesson_name: str
    n: int
    item_id: str
    title: str


FIELDS_MIN = (
    "moduleIds,"
    "onDemandCourseMaterialModules.v1(name,lessonIds),"
    "onDemandCourseMaterialLessons.v1(name,elementIds),"
    "onDemandCourseMaterialItems.v2(name,slug,contentSummary)"
)
# Wider field list (as used by older community tools) - fallback if the minimal one yields nothing.
FIELDS_FULL = (
    "moduleIds,onDemandCourseMaterialModules.v1(name,slug,description,timeCommitment,lessonIds,optional,learningObjectives),"
    "onDemandCourseMaterialLessons.v1(name,slug,timeCommitment,elementIds,optional,trackId),"
    "onDemandCourseMaterialPassableItemGroups.v1(requiredPassedCount,passableItemGroupChoiceIds,trackId),"
    "onDemandCourseMaterialPassableItemGroupChoices.v1(name,description,itemIds),"
    "onDemandCourseMaterialPassableLessonElements.v1(gradingWeight,isRequiredForPassing),"
    "onDemandCourseMaterialItems.v2(name,slug,timeCommitment,contentSummary,isLocked,lockableByOwner,"
    "itemLockSummary,customDisplayTypeId,trackId,lockedStatus,forcedLockedStatus),"
    "onDemandCourseMaterialTracks.v1(passablesCount)"
)


def _parse_structure(data: dict) -> tuple[str, list[Lecture], dict]:
    elements = data.get("elements") or []
    if not elements:
        raise CourseNotFound("empty elements")
    course_id = elements[0]["id"]
    linked = data.get("linked", {})
    modules = {m["id"]: m for m in linked.get("onDemandCourseMaterialModules.v1", [])}
    lessons = {x["id"]: x for x in linked.get("onDemandCourseMaterialLessons.v1", [])}
    items_list = linked.get("onDemandCourseMaterialItems.v2", [])
    items = {i["id"]: i for i in items_list}
    passable = {x["id"]: x for x in linked.get("onDemandCourseMaterialPassableLessonElements.v1", []) if "id" in x}

    def is_lecture(it: Optional[dict]) -> bool:
        return bool(it) and str((it.get("contentSummary") or {}).get("typeName", "")).lower() == "lecture"

    def resolve(eid) -> Optional[dict]:
        """Map a lesson element id to an item, tolerating several id formats."""
        eid = str(eid)
        if eid in items:
            return items[eid]
        for sep in ("~", ":", "|", "/"):
            if sep in eid:
                for part in eid.split(sep):
                    if part in items:
                        return items[part]
        pe = passable.get(eid)
        if pe:
            for key in ("itemId", "elementId", "id"):
                if pe.get(key) in items:
                    return items[pe[key]]
        return None

    types: dict[str, int] = {}
    for it in items_list:
        t = str((it.get("contentSummary") or {}).get("typeName"))
        types[t] = types.get(t, 0) + 1

    sample_lesson = next(iter(lessons.values()), {})
    stats = {
        "linked_keys": sorted(linked.keys()),
        "element_keys": sorted(elements[0].keys()),
        "modules": len(modules),
        "lessons": len(lessons),
        "items": len(items),
        "item_types": types,
        "module_keys": sorted(next(iter(modules.values()), {}).keys()),
        "lesson_keys": sorted(sample_lesson.keys()),
        "item_keys": sorted(items_list[0].keys()) if items_list else [],
        "sample_lesson_element_ids": [str(x)[:60] for x in (sample_lesson.get("elementIds") or sample_lesson.get("itemIds") or [])[:3]],
        "sample_item_ids": [str(i["id"])[:60] for i in items_list[:3]],
    }

    # Preferred: walk module -> lesson -> element, keeping course order.
    result: list[Lecture] = []
    for m_idx, mid in enumerate(elements[0].get("moduleIds", []), 1):
        mod = modules.get(mid)
        if not mod:
            continue
        for l_idx, lid in enumerate(mod.get("lessonIds", []), 1):
            les = lessons.get(lid)
            if not les:
                continue
            n = 0
            for eid in les.get("elementIds") or les.get("itemIds") or []:
                it = resolve(eid)
                if is_lecture(it):
                    n += 1
                    result.append(Lecture(m_idx, mod["name"], l_idx, les["name"], n, it["id"], it["name"]))

    # Fallback: structure links could not be resolved -> take lectures in API order.
    if not result:
        flat = [i for i in items_list if is_lecture(i)]
        if flat:
            log.warning(
                "Warning: module/lesson links resolve nahi hue; %d lectures serial order mein "
                "ek folder mein download hongi.", len(flat)
            )
            for n, it in enumerate(flat, 1):
                result.append(Lecture(1, "All lectures", 1, "Lectures", n, it["id"], it["name"]))
            stats["used_flat_fallback"] = True
    return course_id, result, stats


def fetch_structure(ctx: Context, slug: str, dump_path: Optional[Path] = None) -> tuple[str, list[Lecture]]:
    def request(fields: str) -> dict:
        url = (
            f"{BASE}/api/onDemandCourseMaterials.v2/?q=slug&slug={slug}"
            f"&includes=modules,lessons,passableItemGroups,passableItemGroupChoices,"
            f"passableLessonElements,items,tracks&fields={fields}&showLockedItems=true"
        )
        return api_json(ctx, url)

    data = request(FIELDS_MIN)
    course_id, lectures, stats = _parse_structure(data)
    if not lectures:
        log.debug("minimal fields gave 0 lectures (%s); retrying with full fields", stats)
        data = request(FIELDS_FULL)
        course_id, lectures, stats = _parse_structure(data)

    if dump_path:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("API response saved: %s", dump_path.resolve())
    if not lectures:
        log.error("Diagnostic: %s", json.dumps(stats, ensure_ascii=False))
    return course_id, lectures


def fetch_video_info(ctx: Context, course_id: str, item_id: str) -> Optional[dict]:
    fields = "onDemandVideos.v1(sources,subtitles,subtitlesVtt,subtitlesTxt)"
    url = (
        f"{BASE}/api/onDemandLectureVideos.v1/{course_id}~{item_id}"
        f"?includes=video&fields={fields}"
    )
    data = api_json(ctx, url)
    videos = data.get("linked", {}).get("onDemandVideos.v1", [])
    return videos[0] if videos else None


def pick_video_url(video: dict, wanted: str) -> tuple[Optional[str], Optional[str]]:
    by_res = (video.get("sources") or {}).get("byResolution") or {}

    def height(label: str) -> int:
        m = re.match(r"(\d+)", label)
        return int(m.group(1)) if m else 0

    if by_res.get(wanted, {}).get("mp4VideoUrl"):
        return by_res[wanted]["mp4VideoUrl"], wanted
    for label in sorted(by_res, key=height, reverse=True):
        if by_res[label].get("mp4VideoUrl"):
            return by_res[label]["mp4VideoUrl"], label
    return None, None


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def _total_from_headers(r: requests.Response, start: int) -> Optional[int]:
    cr = r.headers.get("Content-Range")
    if cr:
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1))
    cl = r.headers.get("Content-Length")
    if cl and cl.isdigit():
        return int(cl) + (start if r.status_code == 206 else 0)
    return None


def download_file(ctx: Context, url: str, dest: Path) -> str:
    """
    Download url to dest, resuming a previous .part file when possible.
    Returns 'skipped' or 'downloaded'. Raises ExpiredLink / requests errors.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    session = ctx.sessions.get()
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        if ctx.stop.is_set():
            raise Cancelled()
        start = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={start}-"} if start else {}
        try:
            with session.get(url, stream=True, headers=headers, timeout=(10, 60)) as r:
                if r.status_code in (401, 403, 410):
                    raise ExpiredLink(f"HTTP {r.status_code}")
                if r.status_code == 416:
                    total = _total_from_headers(r, start)
                    if total is not None and total == start:
                        part.replace(dest)
                        return "downloaded"
                    part.unlink(missing_ok=True)  # corrupt partial; start over
                    continue
                r.raise_for_status()

                mode = "ab"
                if r.status_code == 200 and start:
                    mode, start = "wb", 0  # server ignored Range
                total = _total_from_headers(r, start)

                with open(part, mode) as fh:
                    for chunk in r.iter_content(CHUNK):
                        if ctx.stop.is_set():
                            raise Cancelled()
                        if chunk:
                            fh.write(chunk)

            size = part.stat().st_size
            if total is not None and size != total:
                raise IOError(f"size mismatch: {size} != {total}")
            part.replace(dest)
            return "downloaded"
        except (ExpiredLink, Cancelled):
            raise
        except (requests.RequestException, IOError) as exc:
            last_exc = exc
            wait = min(60.0, 2**attempt) + random.uniform(0, 1)
            log.debug("retry %d/%d for %s (%s) in %.1fs", attempt, MAX_DOWNLOAD_ATTEMPTS, dest.name, exc, wait)
            if ctx.stop.wait(wait):
                raise Cancelled()
    raise IOError(f"download fail ho gaya after {MAX_DOWNLOAD_ATTEMPTS} attempts: {last_exc}")


def with_link_refresh(fetch_url: Callable[[], Optional[str]], do_download: Callable[[str], str]) -> str:
    """Run a download; if the signed link expired, fetch a fresh one once."""
    url = fetch_url()
    if not url:
        return "missing"
    try:
        return do_download(url)
    except ExpiredLink:
        url = fetch_url()
        if not url:
            return "missing"
        return do_download(url)


# --------------------------------------------------------------------------- #
# Per-lecture worker
# --------------------------------------------------------------------------- #
@dataclass
class Options:
    resolution: str
    sub_langs: list[str]
    subtitles: bool
    transcripts: bool
    videos: bool


def module_dir(root: Path, lec: Lecture) -> Path:
    return root / f"{lec.module_idx:02d} - {clean(lec.module_name)}"


def lecture_base(root: Path, lec: Lecture) -> Path:
    return module_dir(root, lec) / f"{lec.lesson_idx:02d}.{lec.n:02d} - {clean(lec.title)}"


TITLES_FILENAME = "00 - Lecture titles.txt"


def write_title_files(root: Path, lectures: list[Lecture]) -> int:
    """Write one text file per module listing all of its lecture titles."""
    by_module: dict[int, list[Lecture]] = {}
    for lec in lectures:
        by_module.setdefault(lec.module_idx, []).append(lec)

    written = 0
    for m_idx in sorted(by_module):
        lecs = by_module[m_idx]
        head = lecs[0]
        title = f"Module {head.module_idx:02d}: {head.module_name}"
        lines = [title, "=" * len(title), f"Total lectures: {len(lecs)}", ""]
        current_lesson = None
        for lec in lecs:
            if lec.lesson_idx != current_lesson:
                current_lesson = lec.lesson_idx
                if len(lines) > 4:
                    lines.append("")
                lines.append(f"Lesson {lec.lesson_idx:02d}: {lec.lesson_name}")
            lines.append(f"  {lec.lesson_idx:02d}.{lec.n:02d}  {lec.title}")
        folder = module_dir(root, head)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / TITLES_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
            written += 1
        except OSError as exc:
            log.warning("Titles file nahi ban saki (%s): %s", folder.name, exc)
    return written


def process_lecture(ctx: Context, course_id: str, lec: Lecture, root: Path, opts: Options) -> dict:
    base = lecture_base(root, lec)
    res = {
        "module": lec.module_name,
        "lesson": lec.lesson_name,
        "title": lec.title,
        "item_id": lec.item_id,
        "status": "ok",
        "files": [],
        "notes": [],
    }
    video_path = base.with_name(base.name + ".mp4")  # NOT with_suffix: names contain dots
    sub_wanted = opts.subtitles or opts.transcripts

    # Fast path: nothing to do, avoid any API call.
    if video_path.exists() and video_path.stat().st_size > 0:
        has_sub = any(base.parent.glob(glob_escape(base.name) + ".*.vtt")) or any(
            base.parent.glob(glob_escape(base.name) + ".*.srt")
        )
        if not sub_wanted or has_sub:
            res["status"] = "skipped"
            return res
    elif not opts.videos and not sub_wanted:
        res["status"] = "skipped"
        return res

    cache: dict[str, Optional[dict]] = {}

    def info(refresh: bool = False) -> Optional[dict]:
        if refresh or "v" not in cache:
            cache["v"] = fetch_video_info(ctx, course_id, lec.item_id)
        return cache["v"]

    try:
        video = info()
        if not video:
            res["status"] = "no_video"
            return res

        if opts.videos:
            state = {"first": True}

            def get_video_url() -> Optional[str]:
                v = info(refresh=not state["first"])
                state["first"] = False
                if not v:
                    return None
                url, label = pick_video_url(v, opts.resolution)
                note = f"{opts.resolution} nahi mila, {label} use hua"
                if url and label != opts.resolution and note not in res["notes"]:
                    res["notes"].append(note)
                return url

            outcome = with_link_refresh(get_video_url, lambda u: download_file(ctx, u, video_path))
            if outcome == "missing":
                res["status"] = "no_video"
                return res
            res["files"].append(video_path.name)

        if sub_wanted:
            for lang in opts.sub_langs:
                if opts.subtitles:
                    got = False
                    for key, ext in (("subtitlesVtt", "vtt"), ("subtitles", "srt")):
                        if got:
                            break
                        if lang in (video.get(key) or {}):
                            target = base.with_name(f"{base.name}.{lang}.{ext}")
                            download_file(ctx, _abs(video[key][lang]), target)
                            res["files"].append(target.name)
                            got = True
                if opts.transcripts and lang in (video.get("subtitlesTxt") or {}):
                    target = base.with_name(f"{base.name}.{lang}.txt")
                    download_file(ctx, _abs(video["subtitlesTxt"][lang]), target)
                    res["files"].append(target.name)
        return res
    except (Cancelled, AuthError):
        raise
    except Exception as exc:  # keep going with other lectures
        log.debug("lecture failed", exc_info=True)
        res["status"] = "failed"
        res["notes"].append(f"{type(exc).__name__}: {exc}")
        return res


def _abs(url: str) -> str:
    return BASE + url if url.startswith("/") else url


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# --------------------------------------------------------------------------- #
# CLI / orchestration
# --------------------------------------------------------------------------- #
def setup_logging(log_file: Optional[Path], verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):  # avoid duplicate output if main() runs twice
        log.removeHandler(h)
        h.close()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        log.addHandler(fh)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download an enrolled Coursera course for offline personal use.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("course", help="course slug or full course URL")
    p.add_argument("--cookies", default="cookies.txt", help="Netscape-format cookies file")
    p.add_argument("--out", default="coursera_downloads", help="output directory")
    p.add_argument("--resolution", default="720p", help="preferred video resolution")
    p.add_argument("--sub-langs", default="en", help="comma-separated subtitle languages")
    p.add_argument("--no-subs", action="store_true", help="skip subtitles")
    p.add_argument("--transcripts", action="store_true", help="also save plain-text transcripts")
    p.add_argument("--subs-only", action="store_true", help="skip videos, fetch subtitles/transcripts only")
    p.add_argument("--modules", help="only these module numbers, e.g. 1,3-5")
    p.add_argument("--no-titles-file", action="store_true", help="don't write the per-module lecture titles .txt file")
    p.add_argument("--list", action="store_true", help="print course structure and exit")
    p.add_argument("--workers", type=int, default=2, help="parallel downloads (1-6)")
    p.add_argument("--api-delay", type=float, default=0.5, help="min seconds between API calls")
    p.add_argument("--debug-api", action="store_true", help="save raw course-structure API response (for troubleshooting)")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose console logging")
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    slug = extract_slug(args.course)
    root = Path(args.out) / clean(slug)
    setup_logging(root / "download.log", args.verbose)

    cookie_file = Path(args.cookies)
    if not cookie_file.is_file():
        log.error("Cookies file nahi mili: %s", cookie_file)
        return 2

    workers = max(1, min(6, args.workers))
    try:
        ctx = Context(SessionFactory(cookie_file), RateLimiter(args.api_delay))
        log.info("Course structure la raha hoon: %s", slug)
        course_id, lectures = fetch_structure(
            ctx, slug, root / "api_dump.json" if args.debug_api else None
        )
    except AuthError as exc:
        log.error("Auth error: %s", exc)
        return 3
    except CourseNotFound:
        log.error("Course nahi mila. Slug check karein aur confirm karein ke aap enrolled hain.")
        return 4
    except requests.RequestException as exc:
        log.error("Network error: %s", exc)
        return 5

    allowed = parse_ranges(args.modules)
    if allowed:
        lectures = [lec for lec in lectures if lec.module_idx in allowed]
    if not lectures:
        log.error("Koi lecture nahi mila (modules filter ya course khali hai).")
        return 4

    if args.list:
        current = None
        for lec in lectures:
            if lec.module_idx != current:
                current = lec.module_idx
                log.info("\nModule %02d: %s", lec.module_idx, lec.module_name)
            log.info("  %02d.%02d  %s", lec.lesson_idx, lec.n, lec.title)
        log.info("\nTotal lectures: %d", len(lectures))
        return 0

    root.mkdir(parents=True, exist_ok=True)
    if not args.no_titles_file:
        n_files = write_title_files(root, lectures)
        log.info("Lecture titles files likhi gayi: %d module(s)", n_files)
    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        log.warning("Warning: disk par sirf %s free hai.", human(free))

    opts = Options(
        resolution=args.resolution,
        sub_langs=[s.strip() for s in args.sub_langs.split(",") if s.strip()],
        subtitles=not args.no_subs,
        transcripts=args.transcripts,
        videos=not args.subs_only,
    )

    total = len(lectures)
    log.info("%d lectures, %d worker(s). Output: %s\n", total, workers, root.resolve())
    results: list[dict] = []
    counts = {"ok": 0, "skipped": 0, "no_video": 0, "failed": 0}
    started = time.monotonic()
    fatal: Optional[str] = None

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl")
    futures = {executor.submit(process_lecture, ctx, course_id, lec, root, opts): lec for lec in lectures}
    done = 0
    try:
        for fut in as_completed(futures):
            lec = futures[fut]
            done += 1
            try:
                res = fut.result()
            except AuthError as exc:
                fatal = str(exc)
                ctx.stop.set()
                break
            except Cancelled:
                continue
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            results.append(res)
            tag = {"ok": "OK", "skipped": "skip", "no_video": "no video", "failed": "FAILED"}[res["status"]]
            note = f"  ({'; '.join(res['notes'])})" if res["notes"] else ""
            log.info("[%d/%d] %-8s %02d.%02d %s%s", done, total, tag, lec.lesson_idx, lec.n, lec.title, note)
    except KeyboardInterrupt:
        log.warning("\nCtrl+C: ruk raha hoon, partial files resume ke liye save rahengi...")
        ctx.stop.set()
        fatal = fatal or "interrupted"
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    report = {
        "course": slug,
        "course_id": course_id,
        "version": __version__,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "counts": counts,
        "interrupted_or_fatal": fatal,
        "results": sorted(results, key=lambda r: (r["module"], r["lesson"], r["title"])),
    }
    (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info(
        "\nKhatam: %d downloaded, %d skipped, %d no-video, %d failed (%.0fs).",
        counts["ok"], counts["skipped"], counts["no_video"], counts["failed"], report["elapsed_seconds"],
    )
    log.info("Report: %s", (root / "report.json").resolve())
    if fatal and fatal != "interrupted":
        log.error("Auth error beech mein aaya: %s", fatal)
        return 3
    if fatal == "interrupted":
        return 130
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())