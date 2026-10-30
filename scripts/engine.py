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
KEEP_HOURS = 48                                   # stories older than this leave the app
MAX_STORIES = 350
QUEUE_HOURS = 10                                  # a story not written within this time is dropped
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "420"))
UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Mobile Safari/537.36")
CATEGORIES = ["Politics", "Business", "Kathmandu", "Provinces", "Society", "Sports",
              "Entertainment", "Technology", "Health", "Education", "Diaspora"]
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
                             timeout=20)
            if r.status_code != 200:
                report["error"] = f"{url}: HTTP {r.status_code}"
                continue
            f = feedparser.parse(r.content)
            if not f.entries:
                report["error"] = f"{url}: no stories in feed"
                continue
            items = []
            for e in f.entries[:40]:
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
                              "text": body, "source": src["name"]})
            report.update(ok=True, items=len(items), feed=url, error=None)
            return src["name"], items, report
        except Exception as ex:
            report["error"] = f"{url}: {type(ex).__name__}: {ex}"[:300]
    return src["name"], [], report


def full_text(item):
    """The feed often has only the first lines; read the article page for the whole story."""
    if len(item["text"]) >= 1500 and not item.get("cut"):
        return item["text"]
    try:
        r = requests.get(item["url"], headers={"User-Agent": UA}, timeout=20)
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

    # 1. read every site at the same time
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(read_source, sources))
    status_sources = {}
    fresh = []
    for name, items, rep in results:
        status_sources[name] = rep
        for it in items:
            if it["id"] in seen:
                continue
            seen[it["id"]] = NOW
            if it["ts"] < NOW - (6 if first_run else QUEUE_HOURS) * 3600:
                continue
            fresh.append(it)

    for name, rep in status_sources.items():
        print(f"{'OK ' if rep['ok'] else 'NO '} {name:22} {rep['items']:3} stories  {rep['error'] or rep['feed']}")
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
        queue.append(it)
        known.append((w, it))
        added += 1

    # 3. write stories in English, newest and most-reported first, within this run's time
    queue.sort(key=lambda q: (len(q.get("also", [])), q["ts"]), reverse=True)
    writer = ai.Writer(state.get("ai", {}))
    done_ids = set()
    audit = state.get("audit", [])
    deadline = time.time() + RUN_SECONDS
    i = 0
    while i < len(queue) and time.time() < deadline and not writer.exhausted():
        batch = queue[i:i + ai.BATCH]
        i += len(batch)
        for b in batch:
            b["text"] = full_text(b)
        try:
            out = writer.write(batch)
        except Exception:
            traceback.print_exc()
            break
        for b in batch:
            res = out.get(b["id"])
            if res is None:
                continue
            done_ids.add(b["id"])
            if not res.get("nepal_related", True) or not res.get("body"):
                continue
            audit.append({"id": b["id"], "source": b["source"], "title": b["title"], "text": b["text"][:8000]})
            stories.append({
                "id": b["id"], "url": b["url"], "source": b["source"], "also": b.get("also", []),
                "ts": b["ts"], "cat": res["category"] if res["category"] in CATEGORIES else "Society",
                "headline": res["headline"], "points": res["points"], "summary": res["summary"],
                "body": res["body"], "major": bool(res.get("major")), "orig_title": b["title"],
                "by": res["_model"],
            })
    queue = [q for q in queue if q["id"] not in done_ids]
    for q in queue:              # keep the queue small: the article text is read again when needed
        if len(q["text"]) > 1500:
            q["text"], q["cut"] = q["text"][:1500], True

    # 4. publish
    stories.sort(key=lambda s: s["ts"], reverse=True)
    stories = stories[:MAX_STORIES]
    public = [{k: v for k, v in s.items() if k not in ("orig_title",)} for s in stories]
    save("feed", {"v": 1, "ts": NOW, "stories": public})
    major = [{"id": s["id"], "headline": s["headline"], "cat": s["cat"], "ts": s["ts"]}
             for s in stories if s.get("major") and s["ts"] >= NOW - 24 * 3600][:10]
    save("pulse", {"v": 1, "ts": NOW, "major": major, "count": len(stories)})
    save("state", {"seen": seen, "queue": queue[:600], "ai": writer.state, "audit": audit[-60:],
                   "rejects": (state.get("rejects", []) + writer.rejects)[-20:]})

    status = {
        "run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(NOW)),
        "stories_in_app": len(stories), "new_found": added, "written_now": len(done_ids),
        "waiting": len(queue), "ai": writer.report(), "sources": status_sources,
        "newest_story_minutes_old": round((NOW - stories[0]["ts"]) / 60) if stories else None,
    }
    json.dump(status, open(os.path.join(OUT, "status.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in status.items() if k != "sources"}, indent=1))
    bad = [n for n, r in status_sources.items() if not r["ok"]]
    if bad:
        print("sites not readable this run:", ", ".join(bad))


if __name__ == "__main__":
    main()
