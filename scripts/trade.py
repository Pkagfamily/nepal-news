"""Monthly import & export figures from the Department of Customs (Foreign Trade Statistics).

Customs publishes one Excel file per month with the totals SO FAR in the fiscal year ("Upto Kartik").
A single month = this month's file minus last month's file. The same month last year comes from last
year's files. Nothing is estimated: every number is taken from the official files, and a month is only
published after its tables add up (the commodity table must match the total, and so on).
"""
import io
import os
import re
import time
import urllib.parse

import requests

BASE = "https://www.customs.gov.np"
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"}
MONTHS = ["Shrawan", "Bhadra", "Ashwin", "Kartik", "Mangsir", "Poush", "Magh", "Falgun", "Chaitra",
          "Baisakh", "Jestha", "Asar"]
_ALIASES = {"shrawan": 1, "srawan": 1, "saun": 1, "bhadra": 2, "bhadau": 2, "ashwin": 3, "asoj": 3, "aswin": 3,
            "ashoj": 3, "kartik": 4, "kartick": 4, "mangsir": 5, "mansir": 5, "marga": 5, "poush": 6, "push": 6,
            "paush": 6, "magh": 7, "falgun": 8, "phalgun": 8, "fagun": 8, "chaitra": 9, "chait": 9,
            "baisakh": 10, "baishakh": 10, "jestha": 11, "jeth": 11, "jesth": 11, "asar": 12, "ashadh": 12,
            "asadh": 12, "ashar": 12, "annual": 12}

# Fabric and textile groups (HS headings). Units come from the customs file (SQM, Kg, PCS…).
FABRIC_GROUPS = [
    ("Cotton woven fabric", ["5208", "5209", "5210", "5211", "5212"]),
    ("Synthetic filament fabric", ["5407", "5408"]),
    ("Staple fibre fabric (rayon / polyester mix)", ["5512", "5513", "5514", "5515", "5516"]),
    ("Knitted fabric", ["6001", "6002", "6003", "6004", "6005", "6006"]),
    ("Silk fabric", ["5007"]),
    ("Yarn (cotton, synthetic, viscose)", ["5205", "5206", "5207", "5402", "5403", "5509", "5510", "5511"]),
    ("Women's and girls' garments", ["6104", "6106", "6108", "6204", "6206", "6208", "6211"]),
    ("Shawls, scarves and veils", ["6117", "6214"]),
]


def _get(url, timeout=60):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _abs(href):
    return href if href.startswith("http") else urllib.parse.urljoin(BASE + "/", href.lstrip("/"))


def find_files():
    """Every monthly trade-statistics Excel file on the customs site: [(url, name)]."""
    home = _get(BASE + "/").text
    cats = set(re.findall(r'href="\s*(/category/(?:foreign-trade-statistics[^"\s]*|a-v-20\d\d-0\d\d/?|fts-20\d\d-0\d\d/?))\s*"', home))
    pages = set()
    for c in cats:
        if re.search(r"fts-20[0-7]", c):          # very old years are not needed
            continue
        try:
            t = _get(_abs(c)).text
        except Exception:
            continue
        for href, title in re.findall(r'<a[^>]+href="\s*([^"]*?/content/\d+/[^"]*?)\s*"[^>]*>\s*([^<]{4,200})</a>', t):
            if re.search(r"व्यापार.*तथ्याङ्क|trade statistics", title, re.I):
                pages.add(_abs(href.strip()))
    files = {}
    for p in pages:
        try:
            t = _get(p).text
        except Exception:
            continue
        for href in re.findall(r'href="([^"]+\.xlsx?)"', t, re.I):
            u = _abs(href)
            files[u] = urllib.parse.unquote(u.rsplit("/", 1)[-1])
    return sorted(files.items())


def _month_of(index_text, name):
    """(fiscal year 'YYYY/YY', month number 1-12) from the file's own title line, else its file name."""
    t = index_text or ""
    fy = re.search(r"FY\s*(20\d\d)\s*/\s*(\d\d)", t) or re.search(r"(20\d\d)[_/ -](\d\d)", name)
    seg = re.search(r"\(([^)]*)\)\s*of\s*FY", t)
    words = re.findall(r"[A-Za-z]+", seg.group(1) if seg else "") or re.findall(r"[A-Za-z]+", name)
    months = [_ALIASES[w.lower()] for w in words if w.lower() in _ALIASES]
    if re.search(r"annual|twelve|whole", t + " " + name, re.I) and not seg:
        months = [12]
    if not fy or not months:
        return None
    return f"{fy.group(1)}/{fy.group(2)}", months[-1]


def read_file(data):
    """Reads one customs workbook (cumulative figures, Rs thousands)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    names = {re.sub(r"[^a-z]", "", ws.title.lower()): ws for ws in wb.worksheets}

    def sheet(*keys, no="partner"):
        for k, ws in names.items():
            if all(x in k for x in keys) and no not in k:
                return ws
        raise ValueError(f"sheet {keys} not found")

    index = " ".join(str(c) for r in wb.worksheets[0].iter_rows(max_row=3, values_only=True) for c in r if c)
    d = {"index": index}
    t1 = {}
    for r in sheet("tradedirection").iter_rows(min_row=3, values_only=True):
        if r and len(r) >= 4 and isinstance(r[1], str) and isinstance(r[3], (int, float)):
            t1[r[1].lower()] = (r[2], r[3])
    pick = lambda w: next(v for k, v in t1.items() if k.startswith(w))
    d["imports"], d["exports"] = pick("imports (")[1], pick("exports (")[1]
    d["ly_imports"], d["ly_exports"] = pick("imports (")[0], pick("exports (")[0]

    def table(ws, cols):
        out = []
        for r in ws.iter_rows(min_row=4, values_only=True):
            if r and r[0] not in (None, "") and len(r) >= max(cols) + 1:
                out.append(tuple(r[i] for i in cols))
        return out

    d["chapters"] = {str(c).zfill(2): (desc, float(i or 0), float(e or 0))
                     for c, desc, i, e in table(sheet("chapter"), (0, 1, 2, 3)) if str(c).strip().isdigit()}
    d["countries"] = {str(n).strip(): (float(i or 0), float(e or 0))
                      for _, n, i, e in table(sheet("country"), (0, 1, 2, 3)) if isinstance(n, str) and "total" not in n.lower()}
    imp = {}
    for hs, desc, unit, q, v, rev in table(sheet("imports", "commodity"), (0, 1, 2, 3, 4, 5)):
        hs = str(hs).strip()
        if hs.isdigit():
            imp[hs] = (str(desc).strip(), str(unit or "").strip(), float(q or 0), float(v or 0), float(rev or 0))
    d["imp"] = imp
    exp = {}
    for hs, desc, unit, q, v in table(sheet("exports", "commodity"), (0, 1, 2, 3, 4)):
        hs = str(hs).strip()
        if hs.isdigit():
            exp[hs] = (str(desc).strip(), str(unit or "").strip(), float(q or 0), float(v or 0))
    d["exp"] = exp
    try:
        d["customs"] = {str(n).strip().title(): (float(i or 0), float(e or 0))
                        for _, n, i, _s, e in table(sheet("customs"), (0, 1, 2, 3, 4)) if isinstance(n, str) and "total" not in n.lower()}
    except Exception:
        d["customs"] = {}
    return d


def verify(d):
    """The tables must add up to the totals, or the file is not used. Returns the list of checks passed."""
    checks = []
    si = sum(v[3] for v in d["imp"].values())
    se = sum(v[3] for v in d["exp"].values())
    ci = sum(v[1] for v in d["chapters"].values())
    for label, a, b in (("Imports by goods add up to total imports", si, d["imports"]),
                        ("Exports by goods add up to total exports", se, d["exports"]),
                        ("Imports by chapter add up to total imports", ci, d["imports"])):
        if not b or abs(a - b) > 0.002 * b:
            raise ValueError(f"{label}: {a:.0f} vs {b:.0f}")
        checks.append(label)
    return checks


def bs_name(fy, m):
    """Month name with its Bikram Sambat year: Shrawan–Chaitra are in the first year, Baisakh–Asar in the second."""
    return f"{MONTHS[m - 1]} {int(fy[:4]) + (1 if m >= 10 else 0)}"


def _minus(a, b):
    """Single month = cumulative this month - cumulative last month (b may be None for Shrawan)."""
    return a if b is None else a - b


def build(cum):
    """cum: {(fy, month): parsed file}. Returns the published trade data."""
    keys = sorted(cum)
    if not keys:
        return None

    def prev_key(fy, m):
        return (fy, m - 1) if m > 1 else None

    def ly_key(fy, m):
        y = int(fy[:4]) - 1
        return (f"{y}/{str(y + 1)[2:]}", m)

    def month(fy, m):
        """Single-month figures, or None when the month before is missing."""
        c = cum.get((fy, m))
        if c is None:
            return None
        pk = prev_key(fy, m)
        p = cum.get(pk) if pk else None
        if pk and p is None:
            return None
        out = {"imports": _minus(c["imports"], p and p["imports"]), "exports": _minus(c["exports"], p and p["exports"])}
        imp = {}
        for hs, (desc, unit, q, v, rev) in c["imp"].items():
            pq, pv = (p["imp"].get(hs, (0, 0, 0, 0, 0))[2:4]) if p else (0, 0)
            imp[hs] = (desc, unit, q - pq, v - pv)
        exp = {}
        for hs, (desc, unit, q, v) in c["exp"].items():
            pq, pv = (p["exp"].get(hs, (0, 0, 0, 0))[2:4]) if p else (0, 0)
            exp[hs] = (desc, unit, q - pq, v - pv)
        cty = {n: (i - (p["countries"].get(n, (0, 0))[0] if p else 0), e - (p["countries"].get(n, (0, 0))[1] if p else 0))
               for n, (i, e) in c["countries"].items()}
        cus = {n: (i - (p["customs"].get(n, (0, 0))[0] if p else 0), e - (p["customs"].get(n, (0, 0))[1] if p else 0))
               for n, (i, e) in c.get("customs", {}).items()}
        out.update(imp=imp, exp=exp, countries=cty, customs=cus)
        return out

    series = []
    for fy, m in keys:
        x = month(fy, m)
        if x:
            series.append({"fy": fy, "m": m, "name": bs_name(fy, m),
                           "imports": round(x["imports"]), "exports": round(x["exports"])})
    fy, m = keys[-1]
    cur = month(fy, m)
    if cur is None:
        return None
    ly = month(*ly_key(fy, m))

    def top(d, n, lyd):
        rows = sorted(d.items(), key=lambda kv: -kv[1][3])[:n]
        return [{"hs": hs, "desc": v[0], "unit": v[1], "qty": round(v[2], 1), "value": round(v[3]),
                 "ly_value": round(lyd[hs][3]) if lyd and hs in lyd else None} for hs, v in rows]

    def groups(d, lyd):
        out = []
        for name, heads in FABRIC_GROUPS:
            rows = [(hs, v) for hs, v in d.items() if hs[:4] in heads]
            if not rows:
                continue
            units = {}
            for _, v in rows:
                units[v[1]] = units.get(v[1], 0) + v[3]
            unit = max(units, key=units.get) if units else ""
            val = sum(v[3] for _, v in rows)
            qty = sum(v[2] for _, v in rows if v[1] == unit)
            uval = sum(v[3] for _, v in rows if v[1] == unit)
            g = {"group": name, "hs": heads[0][:2] if len({h[:2] for h in heads}) == 1 else "/".join(sorted({h[:2] for h in heads})),
                 "value": round(val), "unit": unit, "qty": round(qty), "avg": round(uval * 1000 / qty, 2) if qty else None}
            if lyd:
                lrows = [(hs, v) for hs, v in lyd.items() if hs[:4] in heads]
                lval = sum(v[3] for _, v in lrows)
                lq = sum(v[2] for _, v in lrows if v[1] == unit)
                luv = sum(v[3] for _, v in lrows if v[1] == unit)
                g.update(ly_value=round(lval), ly_qty=round(lq), ly_avg=round(luv * 1000 / lq, 2) if lq else None)
            g["lines"] = [{"hs": hs, "desc": v[0], "unit": v[1], "qty": round(v[2]), "value": round(v[3])}
                          for hs, v in sorted(rows, key=lambda kv: -kv[1][3])[:8] if v[3] > 0]
            out.append(g)
        return out

    saree = [{"hs": hs, "desc": v[0], "unit": v[1], "qty": round(v[2]), "value": round(v[3]),
              "ly_value": round(ly["imp"][hs][3]) if ly and hs in ly["imp"] else None}
             for hs, v in cur["imp"].items() if re.search(r"\bsar(?:ee|i)s?\b", v[0], re.I) and v[3] > 0]
    c = cum[(fy, m)]
    latest = {
        "fy": fy, "m": m, "name": bs_name(fy, m),
        "imports": round(cur["imports"]), "exports": round(cur["exports"]),
        "ly_imports": round(ly["imports"]) if ly else None, "ly_exports": round(ly["exports"]) if ly else None,
        "fy_imports": round(c["imports"]), "fy_exports": round(c["exports"]),
        "fy_ly_imports": round(c["ly_imports"]), "fy_ly_exports": round(c["ly_exports"]),
        "top_imports": top(cur["imp"], 10, ly and ly["imp"]),
        "top_exports": top(cur["exp"], 10, ly and ly["exp"]),
        "countries": [{"name": n, "imports": round(i), "exports": round(e),
                       "ly_imports": round(ly["countries"].get(n, (0, 0))[0]) if ly else None}
                      for n, (i, e) in sorted(cur["countries"].items(), key=lambda kv: -kv[1][0])[:12]],
        "customs": [{"name": n, "imports": round(i), "exports": round(e)}
                    for n, (i, e) in sorted(cur["customs"].items(), key=lambda kv: -kv[1][0])[:8]],
        "fabric": groups(cur["imp"], ly and ly["imp"]),
        "saree": sorted(saree, key=lambda x: -x["value"])[:10],
        "checks": c.get("checks", []),
    }
    return {"v": 1, "unit": "Rs thousand", "source": "Department of Customs – Nepal Foreign Trade Statistics",
            "latest": latest, "series": series[-13:]}


def update(state, now, force=False):
    """Checks the customs site (every 6 hours) and rebuilds the figures when a new month appears.
    Returns the new data, or None when nothing changed."""
    t = state.setdefault("trade", {})
    if not force and t.get("checked", 0) > now - 6 * 3600:
        return None
    t["checked"] = now
    try:
        files = find_files()
    except Exception as ex:
        t["error"] = f"listing: {ex}"[:300]
        return None
    names = sorted(n for _, n in files)
    if not force and names == t.get("files"):
        return None
    cum = {}
    bad = []
    for url, name in files:
        try:
            d = read_file(_get(url, 180).content)
            key = _month_of(d["index"], name)
            if not key:
                bad.append(f"{name}: month not recognised")
                continue
            d["checks"] = verify(d)
            if key in cum:                       # a corrected file for the same month: keep the bigger total
                if d["imports"] < cum[key]["imports"]:
                    continue
            cum[key] = d
        except Exception as ex:
            bad.append(f"{name}: {ex}"[:200])
    # only the two latest fiscal years are needed (this year + same months last year)
    fys = sorted({k[0] for k in cum})[-2:]
    cum = {k: v for k, v in cum.items() if k[0] in fys}
    out = build(cum)
    t["problems"] = bad[-10:]
    if out is None:
        t["error"] = "could not build"
        return None
    t["files"] = names
    t["latest"] = out["latest"]["name"]
    t.pop("error", None)
    out["published_check"] = now
    return out
