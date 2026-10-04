#!/usr/bin/env python3
"""
Coursera course downloader (personal use, for courses you are enrolled in).

Usage:
    pip install requests
    python coursera_download.py <course-slug> --cookies cookies.txt

<course-slug> is the part of the URL after /learn/, e.g.
    https://www.coursera.org/learn/machine-learning  ->  machine-learning

cookies.txt = Netscape-format cookies exported from your browser while you
are logged in to coursera.org (e.g. with the "Get cookies.txt LOCALLY"
extension). Do NOT share this file; it gives access to your account.

NOTE: This relies on Coursera's internal web API, which can change at any
time. If it stops working, re-export your cookies first, then check for
API changes.
"""

import argparse
import http.cookiejar
import os
import re
import sys
import time

import requests

BASE = "https://www.coursera.org"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def clean(name: str) -> str:
    """Make a string safe to use as a file/folder name."""
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip().rstrip(".")
    return name[:120] or "untitled"


def make_session(cookie_file: str) -> requests.Session:
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies = jar
    return s


def get_json(s: requests.Session, url: str):
    r = s.get(url, timeout=30)
    if r.status_code in (401, 403):
        sys.exit(
            f"Error {r.status_code}: Coursera ne access nahi diya.\n"
            "Browser mein dobara login karein aur cookies.txt phir se export karein."
        )
    r.raise_for_status()
    return r.json()


def get_course_structure(s: requests.Session, slug: str):
    fields = (
        "moduleIds,"
        "onDemandCourseMaterialModules.v1(name,lessonIds),"
        "onDemandCourseMaterialLessons.v1(name,elementIds),"
        "onDemandCourseMaterialItems.v2(name,slug,contentSummary)"
    )
    url = (
        f"{BASE}/api/onDemandCourseMaterials.v2/?q=slug&slug={slug}"
        "&includes=modules,lessons,items"
        f"&fields={fields}&showLockedItems=true"
    )
    data = get_json(s, url)
    if not data.get("elements"):
        sys.exit("Course nahi mila. Slug check karein aur confirm karein ke aap enrolled hain.")

    course_id = data["elements"][0]["id"]
    module_ids = data["elements"][0].get("moduleIds", [])
    linked = data.get("linked", {})
    modules = {m["id"]: m for m in linked.get("onDemandCourseMaterialModules.v1", [])}
    lessons = {l["id"]: l for l in linked.get("onDemandCourseMaterialLessons.v1", [])}
    items = {i["id"]: i for i in linked.get("onDemandCourseMaterialItems.v2", [])}

    structure = []  # list of (module_name, lesson_name, [lecture items])
    for mid in module_ids:
        mod = modules.get(mid)
        if not mod:
            continue
        for lid in mod.get("lessonIds", []):
            les = lessons.get(lid)
            if not les:
                continue
            lectures = []
            for eid in les.get("elementIds", []):
                it = items.get(eid)
                if it and it.get("contentSummary", {}).get("typeName") == "lecture":
                    lectures.append(it)
            structure.append((mod["name"], les["name"], lectures))
    return course_id, structure


def get_lecture_info(s: requests.Session, course_id: str, item_id: str):
    fields = "onDemandVideos.v1(sources,subtitles,subtitlesVtt,subtitlesTxt)"
    url = (
        f"{BASE}/api/onDemandLectureVideos.v1/{course_id}~{item_id}"
        f"?includes=video&fields={fields}"
    )
    data = get_json(s, url)
    videos = data.get("linked", {}).get("onDemandVideos.v1", [])
    return videos[0] if videos else None


def pick_video_url(video: dict, resolution: str):
    by_res = video.get("sources", {}).get("byResolution", {})
    if resolution in by_res:
        return by_res[resolution].get("mp4VideoUrl")
    # fall back to the best available resolution
    def height(r):
        m = re.match(r"(\d+)", r)
        return int(m.group(1)) if m else 0

    for r in sorted(by_res, key=height, reverse=True):
        url = by_res[r].get("mp4VideoUrl")
        if url:
            print(f"    ({resolution} nahi mila, {r} use kiya)")
            return url
    return None


def download_file(s: requests.Session, url: str, path: str):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("    already downloaded, skip")
        return
    tmp = path + ".part"
    with s.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Download your enrolled Coursera course.")
    ap.add_argument("slug", help="course slug from the URL (after /learn/)")
    ap.add_argument("--cookies", default="cookies.txt", help="Netscape cookies file")
    ap.add_argument("--out", default="coursera_downloads", help="output folder")
    ap.add_argument("--resolution", default="720p", help="360p / 540p / 720p")
    ap.add_argument("--sub-lang", default="en", help="subtitle language code, e.g. en")
    ap.add_argument("--no-subs", action="store_true", help="skip subtitles")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    if not os.path.exists(args.cookies):
        sys.exit(f"Cookies file nahi mili: {args.cookies}")

    s = make_session(args.cookies)
    print("Course structure la raha hoon...")
    course_id, structure = get_course_structure(s, args.slug)

    root = os.path.join(args.out, clean(args.slug))
    os.makedirs(root, exist_ok=True)

    total = sum(len(l) for _, _, l in structure)
    print(f"{total} lectures mile.\n")

    done = 0
    mod_index = {}
    for mod_name, les_name, lectures in structure:
        if mod_name not in mod_index:
            mod_index[mod_name] = len(mod_index) + 1
        mdir = os.path.join(root, f"{mod_index[mod_name]:02d} - {clean(mod_name)}")
        os.makedirs(mdir, exist_ok=True)

        for n, item in enumerate(lectures, 1):
            done += 1
            title = clean(item["name"])
            base = f"{clean(les_name)} - {n:02d} - {title}"
            print(f"[{done}/{total}] {mod_name} / {item['name']}")
            try:
                video = get_lecture_info(s, course_id, item["id"])
                if not video:
                    print("    video info nahi mili, skip")
                    continue

                vurl = pick_video_url(video, args.resolution)
                if vurl:
                    download_file(s, vurl, os.path.join(mdir, base + ".mp4"))
                else:
                    print("    mp4 link nahi mila, skip")

                if not args.no_subs:
                    for key, ext in (("subtitlesVtt", "vtt"), ("subtitles", "srt")):
                        subs = video.get(key) or {}
                        if args.sub_lang in subs:
                            surl = subs[args.sub_lang]
                            if surl.startswith("/"):
                                surl = BASE + surl
                            download_file(
                                s, surl, os.path.join(mdir, f"{base}.{args.sub_lang}.{ext}")
                            )
                            break
            except requests.RequestException as e:
                print(f"    error: {e}")
            time.sleep(args.delay)

    print(f"\nKhatam. Files yahan hain: {os.path.abspath(root)}")


if __name__ == "__main__":
    main()
