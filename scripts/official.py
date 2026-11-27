"""Official notices, read straight from government websites and explained in English.

Customs (incl. every customs office's auctions), Inland Revenue, Nepal Rastra Bank, the Department of
Commerce, Kathmandu Valley Traffic Police and KUKL (Kathmandu water). Each notice's own page and its
attached file (PDF or picture) go to the AI, which also writes out the original Nepali text; every number
in the English must be found in that original, or the notice is not published.
"""
import base64
import hashlib
import html as htmllib
import re
import time
import urllib.parse

import requests

import ai

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"}

SOURCES = [
    {"office": "Customs", "name": "Department of Customs", "cat": "Tax & Customs",
     "lists": ["https://www.customs.gov.np/", "https://www.customs.gov.np/category/foreign-trade-statistics2083-084-"],
     "link": r"/content/\d+/"},
    {"office": "IRD", "name": "Inland Revenue Department", "cat": "Tax & Customs",
     "lists": ["https://ird.gov.np/category/notice/", "https://ird.gov.np/"], "link": r"/content/\d+/"},
    {"office": "NRB", "name": "Nepal Rastra Bank", "cat": "Banking & Finance",
     "feeds": ["https://www.nrb.org.np/category/circulars/feed/", "https://www.nrb.org.np/category/notices/feed/"],
     "keep": r"/fxm/|आयात|निर्यात|भन्सार|प्रतीतपत्र|ब्याज|मौद्रिक|विदेशी विनिमय|कर्जा|import|export|interest|letter of credit|monetary"},
    {"office": "Commerce", "name": "Department of Commerce, Supplies and Consumer Protection", "cat": "Business",
     "lists": ["https://doc.gov.np/category/circular/", "https://doc.gov.np/"], "link": r"/content/\d+/"},
    {"office": "Traffic Police", "name": "Kathmandu Valley Traffic Police", "cat": "Kathmandu", "ktm": True,
     "lists": ["https://traffic.nepalpolice.gov.np/notices/traffic-activities/", "https://traffic.nepalpolice.gov.np/news/"],
     "link": r"/(?:news|notices)/\d+/",
     "keep": r"अपडेट|update|रुट|डाइभर्सन|diversion|बन्द|सडक|ट्राफिक व्यवस्थापन|विशेष ट्राफिक|लिलाम|सवारी आवागमन"},
    {"office": "KUKL", "name": "Kathmandu Upatyaka Khanepani Limited (KUKL)", "cat": "Kathmandu", "ktm": True,
     "lists": ["https://kukl.org.np/publications/notice"], "link": r"/publications/notice/[A-Za-z0-9_.-]+",
     "keep": r"water|पानी|खानेपानी|distribution|interruption|leakage|tanker|वितरण|अवरुद्ध|बन्द"},
]

# Notices that are of no use to a reader (staff matters, office purchases, greetings…)
SKIP = re.compile(r"सरुवा|भर्ना|अन्तर्वार्ता|अन्तरवार्ता|नियुक्ति|दरखास्त|परीक्षा|सक्षमता|तालिम|प्रशिक्षण|शुभकामना|नारा|"
                  r"सम्पर्क नम्बर|सूचना अधिकारी|हकदावी|दावी गर्न|दाबी गर्न|बोलपत्र|दरभाउपत्र|LOI|आसयको|आशयको|खरिद|"
                  r"कार्यालय समय|स्थानान्तरण|vacancy|interview|recruit|transfer|training|tender|procurement|"
                  r"अनुगमन प्रेस विज्ञप्ति|निरीक्षण अधिकृत|मूल्य विश्लेषण प्रतिवेदन|दररेट पेश|प्रेस विज्ञप्ति \(SASEC|स्मरण हस्तान्तरण|पुरस्कार|journalism|पत्रकारिता", re.I)
AUCTION = re.compile(r"लिलाम|auction", re.I)

DOC_SYSTEM = ai.SYSTEM.split("Strict rules:")[0].replace(
    "You receive news articles from Nepali news sites, usually written in Nepali.",
    "You receive an OFFICIAL NOTICE from a Nepal government office: its web page text and usually an attached "
    "file (PDF or picture, often in Nepali, sometimes scanned).") + """
Strict rules:
- First read the attached file and the page, and put the notice's full original text in "original_text"
  exactly as written (Nepali stays Nepali). If nothing readable is attached and the page has no notice text,
  set "readable" false.
- Then explain the notice as a short English news story: what the office has decided or announced, who it
  applies to, from when, and what people must do. Keep every fact and number; add nothing.
- For an auction: what is being auctioned (list the main lots: goods, vehicles, fabric, quantities), where
  and when the goods can be inspected, the bid deadline and how to bid, all exactly as in the notice.
""" + ai.SYSTEM.split("Strict rules:")[1].split('- "body" is the complete story')[0] + """- "body": the full explanation as paragraphs (lists of lots may be summarised by type, but keep every date,
  deadline and amount). "points": 3 to 5 key points. "summary": 2 to 3 sentences. "headline": clear English.
""" + ai.SYSTEM[ai.SYSTEM.index('- "category"'):ai.SYSTEM.index('- Articles marked scope')] + \
    ai.SYSTEM[ai.SYSTEM.index('- "rule_change"'):]

DOC_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "readable": {"type": "BOOLEAN"},
        "original_text": {"type": "STRING"},
        **{k: v for k, v in ai.SCHEMA["properties"]["stories"]["items"]["properties"].items() if k not in ("id", "nepal_related")},
    },
    "required": ["readable", "original_text"] + [k for k in ai.SCHEMA["properties"]["stories"]["items"]["required"]
                                                 if k not in ("id", "nepal_related")],
}


def _get(url, timeout=40):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r


def _clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def oid(url):
    return "of" + hashlib.sha1(url.encode()).hexdigest()[:14]


def list_items(src):
    """[(url, title)] newest first, as the office lists them."""
    out = []
    for f in src.get("feeds", []):
        t = _get(f).text
        for item in re.findall(r"<item>(.*?)</item>", t, re.S):
            title = _clean((re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1])
            link = _clean((re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1])
            if link:
                out.append((link, title))
    for page in src.get("lists", []):
        t = _get(page).text
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(page))
        for href, title in re.findall(r'<a[^>]+href="\s*([^"]+?)\s*"[^>]*>((?:(?!</a>).){8,400})</a>', t, re.S):
            if not re.search(src["link"], href):
                continue
            title = _clean(title)
            if len(title) < 8 or title.lower() in ("read more", "थप पढ्नुहोस्"):
                # KUKL lists "Read More": use the link's own words
                title = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ").strip()
            out.append((urllib.parse.urljoin(base + "/", href), title))
    seen, uniq = set(), []
    for u, ttl in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, ttl))
    return uniq


def wanted(src, title, url):
    if AUCTION.search(title):
        return True                              # every auction is kept (vehicles, goods, fabric, property)
    if SKIP.search(title):
        return False
    keep = src.get("keep")
    return not keep or bool(re.search(keep, title + " " + url, re.I))


def fetch(url):
    """(page text, [(bytes, mime)]) for one notice."""
    t = _get(url).text
    body = re.sub(r"(?s)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", t)
    main = re.search(r'(?s)<(article|main)[^>]*>(.*?)</\1>', body)
    text = _clean(main.group(2) if main else body)[:6000]
    files = []
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    links = re.findall(r'(?:href|src)="([^"]+\.(?:pdf|jpe?g|png|webp))"', t, re.I)
    for f in links:
        if re.search(r"emblem|logo|flag|icon|banner|favicon|qr|footer|header|google|play", f, re.I):
            continue
        u = urllib.parse.urljoin(base + "/", f)
        try:
            r = requests.get(u, headers=UA, timeout=90)
            if r.status_code != 200 or len(r.content) < 2000 or len(r.content) > 15_000_000:
                continue
        except requests.RequestException:
            continue
        ext = u.lower().rsplit(".", 1)[-1]
        mime = {"pdf": "application/pdf", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
        files.append((r.content, mime))
        if len(files) >= 2:
            break
    return text, files


def explain(writer, src, url, title, found):
    """Returns (story or None, reason)."""
    try:
        text, files = fetch(url)
    except Exception as ex:
        return None, f"could not open: {ex}"[:200]
    if not files and len(text) < 200:
        return None, "no notice text"
    parts = [{"text": f"OFFICE: {src['name']}\nTITLE: {title}\nPAGE TEXT:\n{text}"}]
    for data, mime in files:
        parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}})
    res = writer.ask(DOC_SYSTEM, DOC_SCHEMA, parts)
    if res is None:
        return None, "AI busy"
    if not res.get("readable", True):
        return None, "not readable"
    original = (res.get("original_text") or "") + " " + text
    res["nepal_related"] = True
    why = ai.check({"text": original, "title": title, "id": oid(url), "source": src["office"]}, res)
    if why and not why.startswith("shortened") and not why.startswith("too short"):
        return None, why
    tag = res.get("tag") or "none"
    if AUCTION.search(title) or tag == "auction":
        tag = "auction"
    cat = res.get("category") if res.get("category") in ai.CATS else src["cat"]
    if src.get("ktm"):
        cat = "Kathmandu"
    story = {
        "id": oid(url), "url": url, "source": src["name"], "official": src["office"], "also": [],
        "ts": found, "cat": cat, "headline": res["headline"], "points": [p.strip() for p in res.get("points", []) if p.strip()][:5],
        "summary": res.get("summary", ""), "body": [p.strip() for p in res.get("body", []) if p.strip()],
        "major": False, "rule": bool(res.get("rule_change")), "orig_title": title,
        "tag": "" if tag == "none" else tag,
        "auction_type": "" if res.get("auction_type") in (None, "none") or tag != "auction" else res["auction_type"],
        "trader_note": res.get("trader_note", ""), "effective_bs": res.get("effective_bs", ""),
        "keys": [k.strip() for k in res.get("keys", []) if k.strip()][:6], "by": res.get("_model"),
    }
    return story, None


def run(writer, state, now, night=False, limit=4):
    """Checks the offices (every 20 minutes) and explains new notices. Returns new stories and a report."""
    o = state.setdefault("official", {"seen": {}, "queue": []})
    report = {}
    if o.get("checked", 0) <= now - 20 * 60:
        o["checked"] = now
        first = not o["seen"]
        for src in SOURCES:
            try:
                items = list_items(src)
                report[src["office"]] = f"{len(items)} listed"
            except Exception as ex:
                report[src["office"]] = f"error: {ex}"[:160]
                continue
            new = 0
            # notice numbers on the office's site: an old notice that reappears in a list is not news
            num = lambda u: int((re.search(r"/(?:content|news|notices)/(\d+)/", u) or [0, 0])[1])
            top = o.setdefault("max_id", {}).get(src["office"], 0)
            newest = sorted({num(u) for u, _ in items if num(u)}, reverse=True)[:3]
            for u, t in items:
                if u in o["seen"]:
                    continue
                o["seen"][u] = now
                n = num(u)
                if first or src["office"] not in o["max_id"]:
                    # the very first time: only the newest 3 of each office (no flood of old notices)
                    if not (n and n in newest):
                        continue
                elif n and n <= top:
                    continue
                if not wanted(src, t, u):
                    continue
                o["queue"].append({"url": u, "title": t, "office": src["office"], "found": now})
                new += 1
            ids = [num(u) for u, _ in items if num(u)]
            if ids:
                o["max_id"][src["office"]] = max(ids + [top])
            report[src["office"]] += f", {new} new"
        o["seen"] = dict(sorted(o["seen"].items(), key=lambda kv: kv[1])[-3000:])
    out = []
    if night:
        return out, report
    keep = []
    for q in o["queue"]:
        if len(out) >= limit or writer.exhausted():
            keep.append(q)
            continue
        src = next(s for s in SOURCES if s["office"] == q["office"])
        story, why = explain(writer, src, q["url"], q["title"], q["found"])
        if story:
            out.append(story)
        else:
            q["tries"] = q.get("tries", 0) + 1
            report.setdefault("problems", []).append(f"{q['office']}: {q['title'][:60]} – {why}")
            if q["tries"] < 3 and why != "not readable":
                keep.append(q)
    o["queue"] = [q for q in keep if q["found"] >= now - 3 * 86400]
    return out, report
