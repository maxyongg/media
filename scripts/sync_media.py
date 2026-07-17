#!/usr/bin/env python3
"""
Sync new Letterboxd diary entries into films.json and new Goodreads
"read" shelf entries into books.json. Built to run unattended in GitHub
Actions (see .github/workflows/sync-media.yml) but also runs fine locally.

Env vars:
  LETTERBOXD_USERNAME   required to sync films, e.g. "maxyong"
  GOODREADS_USER_ID     required to sync books, e.g. "100097615"
  GOODREADS_RSS_KEY     optional — only needed if the plain shelf RSS URL
                         stops working unauthenticated (see README note in
                         the workflow file for how to find it)

Exit behavior: writes films.json / books.json in place when new entries are
found, and (when run inside GitHub Actions) sets a `changed` step output so
the workflow knows whether to commit.
"""
import os
import re
import sys
import json
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency: beautifulsoup4 (pip install beautifulsoup4)", file=sys.stderr)
    raise

UA = "Mozilla/5.0 (compatible; LedgerSync/1.0; +https://github.com)"
LB_NS = {"letterboxd": "https://letterboxd.com"}


# ---------------------------------------------------------------- helpers --

def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_text(url, timeout=20):
    return fetch(url, timeout=timeout).decode("utf-8", errors="replace")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")


def make_id_generator(items, prefix):
    """Continues numbering after the highest existing id of the given prefix."""
    used = set()
    for it in items:
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", it.get("id", "") or "")
        if m:
            used.add(int(m.group(1)))
    n = (max(used) + 1) if used else 1

    def gen():
        nonlocal n
        while n in used:
            n += 1
        used.add(n)
        val = f"{prefix}{n:04d}"
        n += 1
        return val

    return gen


# ------------------------------------------------------------- letterboxd --

def canonical_film_url(rss_link):
    """The RSS <link> points at the user's own diary-entry page
    (letterboxd.com/{username}/film/{slug}/), which has no director/genre
    info on it. The canonical film page drops the username segment."""
    m = re.match(r"^(https?://letterboxd\.com/)[^/]+/film/([^/]+)/?", rss_link)
    if m:
        return f"{m.group(1)}film/{m.group(2)}/"
    return rss_link


def scrape_letterboxd_film(rss_link):
    """Returns (director, genre) by reading the film's plain HTML pages.
    Mirrors the same selectors proven to work via live DOM queries:
      director: a[href*="/director/"]
      genre:    a[href*="/films/genre/"]  (on the film's /genres/ sub-page)
    """
    film_url = canonical_film_url(rss_link)
    director = "Unknown"
    try:
        html = fetch_text(film_url)
        soup = BeautifulSoup(html, "html.parser")
        dir_links = soup.select('a[href*="/director/"]')
        names = list(dict.fromkeys(
            a.get_text(strip=True) for a in dir_links if a.get_text(strip=True)
        ))
        if names:
            director = " & ".join(names)
    except Exception as e:
        print(f"    (director scrape failed: {e})")

    genre = "Unknown"
    try:
        genres_url = film_url.rstrip("/") + "/genres/"
        ghtml = fetch_text(genres_url)
        gsoup = BeautifulSoup(ghtml, "html.parser")
        genre_links = gsoup.select('a[href*="/films/genre/"]')
        names = list(dict.fromkeys(
            a.get_text(strip=True) for a in genre_links if a.get_text(strip=True)
        ))
        if names:
            genre = "/".join(names[:3])
    except Exception as e:
        print(f"    (genre scrape failed: {e})")

    return director, genre


def sync_letterboxd(username, films_path, log):
    url = f"https://letterboxd.com/{username}/rss/"
    try:
        xml_bytes = fetch(url)
    except (HTTPError, URLError) as e:
        log(f"Letterboxd RSS fetch failed: {e}")
        return False

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"Letterboxd RSS did not parse as XML: {e}")
        return False

    items = root.findall("./channel/item")
    if not items:
        log("Letterboxd RSS: no items returned")
        return False

    data = load_json(films_path)
    existing = data["items"]
    seen = {(i["name"].strip().lower(), i.get("yearReleased")) for i in existing}
    gen_id = make_id_generator(existing, "f")

    added = 0
    for item in items:
        title = item.findtext("letterboxd:filmTitle", namespaces=LB_NS) or item.findtext("title")
        if not title:
            continue
        title = title.strip()

        year_txt = item.findtext("letterboxd:filmYear", namespaces=LB_NS)
        year = int(year_txt) if year_txt and year_txt.strip().isdigit() else None

        key = (title.lower(), year)
        if key in seen:
            continue

        watched_txt = item.findtext("letterboxd:watchedDate", namespaces=LB_NS)
        year_watched = int(watched_txt[:4]) if watched_txt else None

        rating_txt = item.findtext("letterboxd:memberRating", namespaces=LB_NS)
        rating = round(float(rating_txt) * 2) if rating_txt else None

        link = item.findtext("link")
        director, genre = scrape_letterboxd_film(link) if link else ("Unknown", "Unknown")

        entry = {
            "id": gen_id(),
            "name": title,
            "director": director,
            "genre": genre,
            "yearReleased": year,
            "yearWatched": year_watched,
            "rating": rating,
        }
        existing.append(entry)
        seen.add(key)
        added += 1
        log(f"  + {entry['name']} ({year}) - {director} - {genre} - {rating}/10")
        time.sleep(0.5)  # be polite to letterboxd

    if added:
        save_json(films_path, data)
        log(f"films.json: added {added} new film(s)")
    else:
        log("films.json: no new entries")
    return added > 0


# --------------------------------------------------------------- goodreads --

def parse_goodreads_description(desc_html):
    """Goodreads shelf RSS crams most fields as 'label: value' lines inside
    the item's <description> HTML. Pull them out as a dict."""
    soup = BeautifulSoup(desc_html or "", "html.parser")
    text = soup.get_text("\n")
    fields = {}
    for line in text.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k and k not in fields:  # keep first occurrence
                fields[k] = v
    return fields


def scrape_goodreads_genre(book_url):
    """Goodreads book pages are client-rendered, so genre tags aren't in the
    plain HTML — needs a real browser. Uses Playwright headless chromium."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Unknown"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=UA)
            page.goto(book_url, wait_until="networkidle", timeout=25000)
            genres = page.eval_on_selector_all(
                'a.BookPageMetadataSection__genreButton, [data-testid="genresList"] a',
                "els => els.slice(0,3).map(e => e.textContent.trim())"
            )
            browser.close()
            genres = [g for g in genres if g]
            return "/".join(dict.fromkeys(genres)) if genres else "Unknown"
    except Exception as e:
        print(f"    (goodreads genre scrape failed: {e})")
        return "Unknown"


def sync_goodreads(user_id, rss_key, books_path, log):
    url = f"https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
    if rss_key:
        url += f"&key={rss_key}"
    try:
        xml_bytes = fetch(url)
    except (HTTPError, URLError) as e:
        log(f"Goodreads RSS fetch failed: {e}")
        return False

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        log("Goodreads RSS did not return valid XML (likely needs the shelf "
            "made public, or a GOODREADS_RSS_KEY secret). Skipping.")
        return False

    items = root.findall("./channel/item")
    if not items:
        log("Goodreads RSS: no items returned")
        return False

    data = load_json(books_path)
    existing = data["items"]
    seen = {(i["name"].strip().lower(), (i.get("author") or "").strip().lower()) for i in existing}
    gen_id = make_id_generator(existing, "b")

    added = 0
    for item in items:
        title = item.findtext("title")
        link = item.findtext("link")
        fields = parse_goodreads_description(item.findtext("description"))
        author = fields.get("author", "").strip()
        if not title or not author:
            continue
        title = title.strip()

        key = (title.lower(), author.lower())
        if key in seen:
            continue

        pub_txt = fields.get("book published", "")
        year_published = int(pub_txt) if pub_txt.isdigit() else None

        rating_txt = fields.get("rating", "")
        rating = int(rating_txt) if rating_txt.isdigit() else None

        read_txt = fields.get("read at", "")
        year_read = int(read_txt[:4]) if re.match(r"^\d{4}", read_txt) else None

        genre = scrape_goodreads_genre(link) if link else "Unknown"

        entry = {
            "id": gen_id(),
            "name": title,
            "author": author,
            "genre": genre,
            "yearPublished": year_published,
            "yearRead": year_read,
            "rating": rating,
        }
        existing.append(entry)
        seen.add(key)
        added += 1
        log(f"  + {entry['name']} - {author} - {genre} - {rating}/5")

    if added:
        save_json(books_path, data)
        log(f"books.json: added {added} new book(s)")
    else:
        log("books.json: no new entries")
    return added > 0


# --------------------------------------------------------------------- main --

def main():
    changed = False

    def log(msg):
        print(msg, flush=True)

    lb_user = os.environ.get("LETTERBOXD_USERNAME", "").strip()
    gr_user = os.environ.get("GOODREADS_USER_ID", "").strip()
    gr_key = os.environ.get("GOODREADS_RSS_KEY", "").strip()

    if lb_user:
        log(f"== Letterboxd: {lb_user} ==")
        if sync_letterboxd(lb_user, "films.json", log):
            changed = True
    else:
        log("LETTERBOXD_USERNAME not set - skipping films sync")

    if gr_user:
        log(f"== Goodreads: {gr_user} ==")
        if sync_goodreads(gr_user, gr_key, "books.json", log):
            changed = True
    else:
        log("GOODREADS_USER_ID not set - skipping books sync")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")

    log("done" + (" - changes written" if changed else " - nothing new"))


if __name__ == "__main__":
    main()
