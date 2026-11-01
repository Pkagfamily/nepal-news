"""Nepal News engine.

Every run (about every 10 minutes on GitHub's servers):
  1. reads the news sites listed in sources.json,
  2. finds stories it has not seen before (skipping the same story told by several sites),
  3. has an AI model write each one as a proper English news story
     (headline, key points, short summary, full story),
  4. publishes the result as one locked (encrypted) and signed file that only the app can open.

Secret (GitHub > Settings > Secrets): NEWS_SECRET = base64 JSON of GEMINI_KEY, FEED_KEY, SIGN_KEY. GITHUB_TOKEN is given by GitHub.
"""
import base64
import calendar
import gzip
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests
import trafilatura
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# The three keys arrive as one bundled GitHub secret (NEWS_SECRET) to keep setup to one paste.
if os.environ.get("NEWS_SECRET"):
    for _k, _v in json.loads(base64.b64decode(os.environ["NEWS_SECRET"])).items():
        os.environ.setdefault(_k, _v)

import ai  # noqa: E402  (reads GEMINI_KEY when used)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUT_DIR", "out")            # the published branch is checked out here
NOW = int(time.time())
KEEP_HOURS = 72                                   # the app's main list: 3 days (older days: archive)
MAX_STORIES = 2400                                 # 3 full days of news from all sites
QUEUE_HOURS = 40                                  # every story found in the last ~2 days gets written
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "400"))
UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Mobile Safari/537.36")
MONEY_SITES = {"ShareSansar", "Mero Lagani", "Bizmandu", "Arthasarokar", "Aarthik News", "Clickmandu",
               "Karobar Daily", "Abhiyan Daily", "Arthik Abhiyan", "New Business Age", "Share Hub Nepal"}
MONEY_WORDS = re.compile(r"भन्सार|महसुल|भ्याट|मूल्य अभिवृद्धि कर|अन्तःशुल्क|आयकर|आयात|निर्यात|कपडा|"
                         r"customs|duty|\bVAT\b|excise|import|export|textile|सुन|चाँदी|चादी|पेट्रोल|डिजेल|ग्यास|इन्धन|आयल निगम|मट्टितेल|बैंक|ब्याज|कर्जा|नेप्से|शेयर|"
                         r"आईपीओ|आइपीओ|लाभांश|बोनस|धितोपत्र|सेबोन|सहकारी|बीमा|विप्रेषण|मुद्रास्फीति|बजेट|"
                         r"gold|silver|petrol|diesel|\bLPG\b|fuel|\bNOC\b|bank|interest rate|loan|NEPSE|share|IPO|"
                         r"dividend|SEBON|cooperative|insurance|remittance|inflation|budget", re.I)
CATEGORIES = ["Politics", "Share Market", "Gold & Silver", "Fuel & Oil", "Tax & Customs", "Banking & Finance", "Business", "Automobile", "Auto · India & World", "Economy", "Kathmandu", "Provinces", "Crime", "Weather & Disasters", "Society", "Health", "Education", "Sports", "Entertainment", "Technology", "Tourism", "Agriculture", "Energy", "Infrastructure", "Diaspora", "World & Nepal"]
SKIP_TITLE = re.compile(r"राशिफल|horoscope|आजको राशि|भिडियो|video|फोटो फिचर|photo feature|"
                        r"तस्बिरहरू|in pictures|quiz|crossword|आजको मौसम", re.I)
SKIP_TAGS = re.compile(r"^(विश्व|अन्तर्राष्ट्रिय|international|world|global|विदेश|horoscope|राशिफल|video|भिडियो)$", re.I)


# ------------------------------------------------------------------ locking + signing
def _key(name):
    v = os.environ.get(name, "")
    if not v:
        sys.exit(f"missing secret {name}")
    return base64.b64decode(v)


def seal(kind, obj):
    """gzip -> AES-256-GCM -> Ed25519 signature. The app checks the signature before opening."""
    ts = NOW
    raw = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    nonce = os.urandom(12)
    aad = f"nn1|{ts}|{kind}".encode()
    ct = AESGCM(_key("FEED_KEY")).encrypt(nonce, raw, aad)
    sig = Ed25519PrivateKey.from_private_bytes(_key("SIGN_KEY")).sign(aad + b"|" + nonce + ct)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "kind": kind, "ts": ts, "n": b(nonce), "c": b(ct), "s": b(sig)})


def unseal(kind, text):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa
    e = json.loads(text)
    nonce, ct, sig = (base64.b64decode(e[k]) for k in ("n", "c", "s"))
    aad = f"nn1|{e['ts']}|{kind}".encode()
    Ed25519PrivateKey.from_private_bytes(_key("SIGN_KEY")).public_key().verify(sig, aad + b"|" + nonce + ct)
    return json.loads(gzip.decompress(AESGCM(_key("FEED_KEY")).decrypt(nonce, ct, aad)))


def load(kind, default):
    p = os.path.join(OUT, f"{kind}.bin")
    if not os.path.exists(p):
        return default
    try:
        return unseal(kind, open(p).read())
    except Exception as ex:  # a broken file must never stop the news
        print(f"could not open {kind}.bin: {ex}")
        return default


def save(kind, obj):
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, f"{kind}.bin.tmp")
    open(tmp, "w").write(seal(kind, obj))
    os.replace(tmp, os.path.join(OUT, f"{kind}.bin"))


# ------------------------------------------------------------------ text helpers
TAG = re.compile(r"<[^>]+>")


def clean(s):
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", s or "")
    s = html.unescape(TAG.sub(" ", s))
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def words(s):
    return {w for w in re.findall(r"[\wऀ-ॿ]+", (s or "").lower()) if len(w) > 1}


def similar(a, b, need=0.5):
    if not a or not b:
        return False
    return len(a & b) / max(1, len(a | b)) >= need


def sid(url):
    u = re.sub(r"[?#].*$", "", url.strip()).rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u)
    return hashlib.sha1(u.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ reading the sites
def read_source(src):
    """Returns (name, items, report). Each item: id, url, title, ts, text, source."""
    report = {"ok": False, "items": 0, "feed": None, "error": None}
    for url in src["feeds"]:
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,*/*"},
                             timeout=12)
            if r.status_code != 200:
                report["error"] = f"{url}: HTTP {r.status_code}"
                continue
            f = feedparser.parse(r.content)
            if not f.entries:
                report["error"] = f"{url}: no stories in feed"
                continue
            items = []
            for e in f.entries[:12 if src.get("scope") == "world" else 40]:
                link = e.get("link") or ""
                title = clean(e.get("title", ""))
                if not link or not title or SKIP_TITLE.search(title):
                    continue
                tags = [t.get("term", "").strip() for t in e.get("tags", []) or []]
                if tags and all(SKIP_TAGS.match(t) for t in tags if t):
                    continue
                tp = e.get("published_parsed") or e.get("updated_parsed")
                ts = calendar.timegm(tp) if tp else NOW
                ts = min(ts, NOW)
                body = ""
                if e.get("content"):
                    body = clean(" ".join(c.get("value", "") for c in e["content"]))
                if len(body) < 400:
                    body = max(body, clean(e.get("summary", "")), key=len)
                items.append({"id": sid(link), "url": link, "title": title, "ts": ts,
                              "text": body, "source": src["name"],
                              "money": bool(src.get("section") == "business" or src["name"] in MONEY_SITES
                                            or MONEY_WORDS.search(title)),
                              **({"scope": "world"} if src.get("scope") == "world" else {})})
            report.update(ok=True, items=len(items), feed=url, error=None,
                          sample=[f'{i["title"][:50]} ({(NOW - i["ts"]) / 3600:.0f}h old)' for i in items[:3]])
            return _label(src), items, report
        except Exception as ex:
            report["error"] = f"{url}: {type(ex).__name__}: {ex}"[:300]
    return _label(src), [], report


def read_page(src):
    """For sites without a feed: read the news list page and take the article links."""
    report = {"ok": False, "items": 0, "feed": None, "error": None}
    for pg in src["pages"]:
        try:
            r = requests.get(pg["url"], headers={"User-Agent": UA}, timeout=20)
            if r.status_code != 200:
                report["error"] = f"{pg['url']}: HTTP {r.status_code}"
                continue
            items, got = [], set()
            for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
                link, title = html.unescape(m.group(1)), clean(m.group(2))
                if not re.search(pg["link"], link) or len(title) < 15 or SKIP_TITLE.search(title):
                    continue
                if link.startswith("/"):
                    link = re.match(r"https?://[^/]+", pg["url"]).group(0) + link
                if link in got:
                    continue
                got.add(link)
                # no feed = no reliable time: the real date is read from the article page before writing
                items.append({"id": sid(link), "url": link, "title": title, "ts": NOW, "text": "",
                              "source": src["name"], "money": True, "nodate": True})
                if len(items) >= 30:
                    break
            if items:
                report.update(ok=True, items=len(items), feed=pg["url"], error=None)
                return _label(src), items, report
            report["error"] = f"{pg['url']}: no article links found"
        except Exception as ex:
            report["error"] = f"{pg['url']}: {type(ex).__name__}: {ex}"[:300]
    return _label(src), [], report


SEARCH_WORDS = ["गाडी", "कार", "बाइक", "स्कुटर", "इभी", "विद्युतीय गाडी", "अटो", "electric vehicle", "car launch",
                "भन्सार", "भन्सार महसुल", "भ्याट", "अन्तःशुल्क", "आयात", "कपडा", "customs duty", "सुनको मूल्य", "चाँदी", "नेप्से", "शेयर बजार", "पेट्रोल", "डिजेल", "ग्यास", "आयल निगम",
                "राष्ट्र बैंक", "ब्याजदर", "बैंक", "आईपीओ", "gold price", "NEPSE", "petrol", "Nepal Rastra Bank"]


def search_sweep(sources, state, ok_sites):
    """Every 30 minutes: search each WordPress news site for money news of the last 2 days, so stories
    that already left a site's main feed are not missed."""
    out = []
    # only sites that answered this round; 8 search words per sweep, taking turns through the list
    bases = sorted({re.match(r"https?://[^/]+", ok_sites[s["name"]]).group(0) for s in sources
                    if s.get("search") and ok_sites.get(s["name"])})
    k = state.get("sweep_at", 0) % len(SEARCH_WORDS)
    words = (SEARCH_WORDS + SEARCH_WORDS)[k:k + 8]
    state["sweep_at"] = k + 8
    jobs = [(b, w) for b in bases for w in words]

    def one(job):
        base, word = job
        name = next(n for n, f in ok_sites.items() if f and f.startswith(base))
        src = {"name": name, "feeds": [f"{base}/?s={requests.utils.quote(word)}&feed=rss2"], "section": None}
        _, items, _ = read_source(src)
        for it in items:
            it["money"] = True
        return items

    with ThreadPoolExecutor(16) as ex:
        for items in ex.map(one, jobs):
            out.extend(i for i in items if i["ts"] >= NOW - 48 * 3600)
    return out


def nepal_day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + 20700))      # Nepal time is UTC+5:45


def archive_days(public, state):
    """Each finished day (2 days after it ends, so late stories are in) is saved once, permanently, as a
    locked file in the project's Releases (one release per month). Releases do not count towards the
    project's size limit, so years of news fit."""
    today = nepal_day(NOW)
    done = set(state.setdefault("archived", []))
    days = sorted({nepal_day(x["ts"]) for x in public})
    for day in days:
        age = (time.mktime(time.strptime(today, "%Y-%m-%d")) - time.mktime(time.strptime(day, "%Y-%m-%d"))) / 86400
        if day in done or age < 2:
            continue
        items = [x for x in public if nepal_day(x["ts"]) == day]
        path = os.path.join(OUT, f"{day}.bin")
        open(path, "w").write(seal("day", {"v": 1, "day": day, "stories": items}))
        tag = "archive-" + day[:7]
        env = dict(os.environ, GH_TOKEN=os.environ.get("GITHUB_TOKEN", ""))
        repo = os.environ.get("REPO", "Pkagfamily/nepal-news")
        import subprocess
        try:
            subprocess.run(["gh", "release", "create", tag, "-R", repo, "--title", f"News archive {day[:7]}",
                            "--notes", "Locked daily news files used by the app's date view."],
                           env=env, capture_output=True, timeout=60)
            r = subprocess.run(["gh", "release", "upload", tag, path, "--clobber", "-R", repo],
                               env=env, capture_output=True, text=True, timeout=120)
        except Exception as ex:
            print(f"archive {day} failed: {ex}")
            continue
        finally:
            os.remove(path)
        if r.returncode == 0:
            state["archived"].append(day)
            print(f"archived {day}: {len(items)} stories")
        else:
            print(f"archive {day} failed: {r.stderr[:200]}")


def _label(src):
    return src["name"] + (f" ({src['section']})" if src.get("section") else "")


def page_date(html_text, url):
    """Publish date of an article (from the page itself, or the date in its address)."""
    try:
        md = trafilatura.extract_metadata(html_text)
        if md is not None and md.date:
            return md.date[:10]
    except Exception:
        pass
    m = re.search(r"(20\d\d)[-/](\d\d)[-/](\d\d)", url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def set_date(item, day):
    """Story time from a date only: today (Nepal time) -> when first seen; earlier day -> noon that day."""
    nepal_today = time.strftime("%Y-%m-%d", time.gmtime(NOW + 20700))
    if day == nepal_today:
        item["ts"] = min(item["ts"], NOW)
    else:
        y, mo, d = (int(x) for x in day.split("-"))
        item["ts"] = calendar.timegm((y, mo, d, 6, 15, 0))          # 12:00 Nepal time
    item.pop("nodate", None)


def full_text(item):
    """The feed often has only the first lines; read the article page for the whole story."""
    if len(item["text"]) >= 1500 and not item.get("cut") and len(item["text"]) != 1500 and not item.get("nodate"):
        return item["text"]
    try:
        r = requests.get(item["url"], headers={"User-Agent": UA}, timeout=20)
        if r.status_code == 200 and item.get("nodate"):
            day = page_date(r.text, item["url"])
            if day:
                set_date(item, day)
        if r.status_code == 200:
            t = trafilatura.extract(r.text, include_comments=False, include_tables=False,
                                    favor_recall=True) or ""
            if len(t) > len(item["text"]):
                return t.strip()
    except Exception:
        pass
    return item["text"]


# ------------------------------------------------------------------ main
def main():
    sources = json.load(open(os.path.join(HERE, "sources.json")))["sources"]
    feed = load("feed", {"stories": []})
    state = load("state", {"seen": {}, "queue": [], "ai": {}})
    stories = [s for s in feed.get("stories", []) if s["ts"] >= NOW - KEEP_HOURS * 3600]
    seen = {k: v for k, v in state.get("seen", {}).items() if v >= NOW - 4 * 86400}
    queue = [q for q in state.get("queue", []) if q["ts"] >= NOW - QUEUE_HOURS * 3600]
    first_run = not stories and not seen

    # one-time clean-up (v1): stories from feed-less sites had no real date; re-check amounts against originals
    if not state.get("cleanup_v1"):
        page_sites = {"ShareSansar", "Mero Lagani", "NEPSE Trading", "Bizshala"}
        originals = {a["id"]: a for a in state.get("audit", [])}
        keep = []
        for st in stories:
            bad = st["source"] in page_sites or "Binance" in st["headline"]
            o = originals.get(st["id"])
            if not bad and o:
                bad = ai.check({"text": o["text"], "title": o["title"], "id": st["id"], "source": st["source"]},
                               {"headline": st["headline"], "body": st["body"], "summary": st["summary"],
                                "points": st["points"], "nepal_related": True}) is not None
            if not bad and re.search(r"\b\d+(\.\d+)?\s*trillion", " ".join(st["body"])) and not o:
                bad = True                             # cannot re-check: leave it out rather than risk it
            if bad:
                seen.pop(st["id"], None)               # read again and written fresh with the new checks
            else:
                keep.append(st)
        print(f"clean-up: removed {len(stories) - len(keep)} stories for re-writing")
        stories = keep
        state["cleanup_v1"] = True

    # 1. read every site at the same time
    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(lambda x: read_page(x) if x.get("pages") else read_source(x), sources))
    if state.get("last_search", 0) < NOW - 30 * 60:
        try:
            ok_sites = {n: r["feed"] for n, r in ((name, rep) for name, _, rep in results) if r["ok"]}
            found = search_sweep(sources, state, ok_sites)
            results.append(("Search sweep (money news, last 2 days)", found,
                            {"ok": True, "items": len(found), "feed": "site searches", "error": None}))
        except Exception as ex:
            print("search sweep failed:", ex)
        state["last_search"] = NOW
    status_sources = {}
    fresh = []
    for name, items, rep in results:
        status_sources[name] = rep
        for it in items:
            if it["id"] in seen:
                continue
            seen[it["id"]] = NOW
            if it["ts"] < NOW - QUEUE_HOURS * 3600:
                continue
            fresh.append(it)

    for name, rep in status_sources.items():
        print(f"{'OK ' if rep['ok'] else 'NO '} {name:22} {rep['items']:3} stories  {rep['error'] or rep['feed']}"
              f"  {' / '.join(rep.get('sample', []))}")
    print(f"new stories found: {len(fresh)}")

    # 2. the same story from several sites is written once; the others are listed as "also in"
    known = [(words(s.get("orig_title", "")), s) for s in stories] + [(words(q["title"]), q) for q in queue]
    added = 0
    for it in sorted(fresh, key=lambda x: x["ts"]):
        w = words(it["title"])
        twin = next((s for (sw, s) in known if similar(w, sw)
                     and abs(s["ts"] - it["ts"]) < 18 * 3600), None)
        if twin is not None:
            also = twin.setdefault("also", [])
            if it["source"] not in also and it["source"] != twin["source"]:
                also.append(it["source"])
            continue
        it["found"] = NOW
        queue.append(it)
        known.append((w, it))
        added += 1

    # 3. write stories in English, newest and most-reported first, within this run's time
    # newest first; widely reported stories and money news (shares, gold, fuel, banking) get priority
    # money news (shares, gold, fuel, banking) first, then stories reported by several sites, then newest
    # brand-new news first (last 90 minutes), then money news, then widely reported, then newest; world auto last
    queue.sort(key=lambda q: (q.get("scope") != "world", q["ts"] >= NOW - 90 * 60, bool(q.get("money")),
                              len(q.get("also", [])) > 0, q["ts"]), reverse=True)
    writer = ai.Writer(state.get("ai", {}))
    writers = [writer] + [ai.Writer(writer.state, start=k) for k in (1, 2)]   # up to 3 writers in parallel
    probe = state.get("gh_probe") or {}
    if probe.get("at", 0) < NOW - (3600 if probe.get("code") == 200 else 900) and os.environ.get("GITHUB_TOKEN"):
        try:                                   # hourly check that GitHub's free AI (the backup) answers
            code, info, out = writer._call("github", "openai/gpt-4o-mini",
                                           ai.prompt([{"id": "t1", "source": "test", "title": "परीक्षण",
                                                       "text": "काठमाडौं । आज मौसम सफा छ।"}]))
            state["gh_probe"] = {"at": NOW, "code": code, "info": str(info)[:300], "reply": str(out)[:120]}
        except Exception as ex:
            state["gh_probe"] = {"at": NOW, "code": -1, "info": f"{type(ex).__name__}: {ex}"[:300]}
    done_ids = set()
    audit = state.get("audit", [])
    deadline = time.time() + RUN_SECONDS
    lock = threading.Lock()
    skipped = []
    batches = [queue[i:i + ai.BATCH] for i in range(0, len(queue), ai.BATCH)]
    if batches and len(batches[-1]) < ai.BATCH and min(q.get("found", 0) for q in batches[-1]) > NOW - 6 * 60:
        batches = batches[:-1]               # not full yet and still fresh: wait for more (saves the allowance)
    # world auto news: at most 25 a day, so Nepal news always has the allowance
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW - 8 * 3600))
    stats = state.setdefault("daily", {}).setdefault(day, {"requests": 0, "stories": 0, "world": 0, "exhausted": {}})
    if stats["world"] >= 25:
        batches = [[q for q in b if q.get("scope") != "world"] for b in batches]
        batches = [b for b in batches if b]
    nxt = iter(batches)

    def add(b, res):
        if not res.get("nepal_related", True) or not res.get("body"):
            return
        hw = words(res["headline"])
        twin = next((x for x in stories if abs(x["ts"] - b["ts"]) < 18 * 3600
                     and similar(hw, words(x["headline"]), 0.45)), None)
        if twin is not None:                       # same story already written from another site
            if b["source"] != twin["source"] and b["source"] not in twin.setdefault("also", []):
                twin["also"].append(b["source"])
            return
        if ai.is_english(b["text"]):
            res["body"] = ai.english_body(b["text"]) or res["body"]
        audit.append({"id": b["id"], "source": b["source"], "title": b["title"], "text": b["text"][:8000]})
        stories.append({
            "id": b["id"], "url": b["url"], "source": b["source"], "also": b.get("also", []),
            "ts": b["ts"], "cat": "Auto · India & World" if b.get("scope") == "world" else
                   (res["category"] if res["category"] in CATEGORIES else "Society"),
            "headline": res["headline"], "points": res["points"], "summary": res["summary"],
            "body": res["body"], "major": bool(res.get("major")), "rule": bool(res.get("rule_change")),
            "orig_title": b["title"],
            "by": res["_model"], "at": NOW,
        })

    def work(w):
        while time.time() < deadline and not w.exhausted():
            with lock:
                batch = next(nxt, None)
            if batch is None:
                return
            for b in batch:
                b["text"] = full_text(b)
            ok = [b for b in batch if not b.get("nodate") and b["ts"] >= NOW - KEEP_HOURS * 3600 and b["text"]]
            with lock:
                for b in batch:
                    if b not in ok:
                        done_ids.add(b["id"])            # no confirmed date, too old, or unreadable: left out
                        skipped.append(f"{b['source']}: {b['title'][:60]}")
            batch = ok
            if not batch:
                continue
            try:
                out = w.write(batch)
            except Exception:
                traceback.print_exc()
                continue
            with lock:
                for b in batch:
                    if b["id"] in out:
                        done_ids.add(b["id"])
                        add(b, out[b["id"]])

    need = 1 if len(batches) <= 3 else 2 if len(batches) <= 8 else 3
    with ThreadPoolExecutor(need) as ex:
        list(ex.map(work, writers[:need]))
    for w in writers:                              # daily usage record (kept 7 days, for checking)
        stats["requests"] += w.log["requests"]
        stats["stories"] += w.log["stories"]
        for m, t in w.state.get("blocked", {}).items():
            if t > NOW + 3600 and m not in stats["exhausted"]:
                stats["exhausted"][m] = time.strftime("%H:%M NPT", time.gmtime(NOW + 20700))
    stats["world"] += sum(1 for s in stories if s.get("at") == NOW and s.get("cat") == "Auto · India & World")
    state["daily"] = dict(sorted(state["daily"].items())[-7:])
    for w in writers[1:]:                          # one combined report
        for k in ("requests", "stories"):
            writer.log[k] += w.log[k]
        writer.log["rejected"] += w.log["rejected"]
        writer.log["errors"] += w.log["errors"]
        writer.rejects += w.rejects
    queue = [q for q in queue if q["id"] not in done_ids]
    for q in queue:              # keep the queue small: the article text is read again when needed
        if len(q["text"]) > 1500:
            q["text"], q["cut"] = q["text"][:1500], True

    # 4. publish
    stories.sort(key=lambda s: s["ts"], reverse=True)
    stories = stories[:MAX_STORIES]
    public = [{k: v for k, v in s.items() if k not in ("orig_title",)} for s in stories]
    save("feed", {"v": 1, "ts": NOW, "stories": public})
    # small file with only what changed in the last 6 hours + the list of current stories, so the app
    # does not download all 3 days every time
    save("latest", {"v": 1, "ts": NOW, "since": NOW - 6 * 3600,
                    "stories": [x for x in public if x.get("at", 0) >= NOW - 6 * 3600],
                    "ids": [x["id"] for x in public]})
    archive_days(public, state)
    major = [{"id": s["id"], "headline": s["headline"], "cat": s["cat"], "ts": s["ts"]}
             for s in stories if s.get("major") and s["ts"] >= NOW - 24 * 3600][:10]
    business = [{"id": s["id"], "headline": s["headline"], "cat": s["cat"], "ts": s["ts"]}
                for s in stories if s.get("rule") and s.get("at", 0) >= NOW - 24 * 3600][:20]
    save("pulse", {"v": 1, "ts": NOW, "major": major, "business": business, "count": len(stories)})
    save("state", {"daily": state.get("daily", {}), "gh_probe": state.get("gh_probe"), "sweep_at": state.get("sweep_at", 0), "seen": seen, "queue": queue[:900], "ai": writer.state, "last_search": state.get("last_search", 0), "cleanup_v1": True,
                   "archived": state.get("archived", [])[-800:], "audit": audit[-60:],
                   "rejects": (state.get("rejects", []) + writer.rejects)[-20:]})

    status = {
        "run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(NOW)),
        "stories_in_app": len(stories), "new_found": added, "written_now": len(done_ids),
        "today": state.get("daily", {}).get(time.strftime("%Y-%m-%d", time.gmtime(NOW - 8 * 3600))),
        "backup_ai_test": state.get("gh_probe"),
        "waiting": len(queue), "left_out_old_or_undated": skipped[-20:], "ai": writer.report(), "sources": status_sources,
        "newest_story_minutes_old": round((NOW - stories[0]["ts"]) / 60) if stories else None,
    }
    json.dump(status, open(os.path.join(OUT, "status.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in status.items() if k != "sources"}, indent=1))
    bad = [n for n, r in status_sources.items() if not r["ok"]]
    if bad:
        print("sites not readable this run:", ", ".join(bad))


if __name__ == "__main__":
    main()
