"""Writes Nepali news as proper English news stories.

Main writer: Google Gemini (free tier, key in GEMINI_KEY).
Backup: GitHub's own AI models (no key needed, uses the workflow's GITHUB_TOKEN).
Each model's daily allowance is tracked; when one is used up the next one takes over.
Every story is checked before it is accepted: all numbers in the English story must exist in
the original article, and the story must not be cut short.
"""
import json
import os
import re
import time

import requests

BATCH = 3                      # stories per AI request (saves the daily request allowance)
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
GH_MODELS = "https://models.github.ai/inference/chat/completions"
GH_LIST = ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"]
CATS = ["Politics", "Business", "Kathmandu", "Provinces", "Society", "Sports",
        "Entertainment", "Technology", "Health", "Education", "Diaspora"]

SYSTEM = """You are a senior editor at an English-language newspaper in Kathmandu.
You receive news articles from Nepali news sites, usually written in Nepali.
For each article, write the story again as a natural, fluent English news report, the way
The Kathmandu Post or Reuters would write it. Do NOT translate word for word: explain the news
clearly so an English reader fully understands what happened, who is involved, why it matters
and what happens next.

Strict rules:
- Keep EVERY fact from the article. Never add facts, guesses, opinions or background that is not in the article.
- Copy every number exactly as in the article (amounts, counts, ages, percentages, dates). Write numbers
  with Western digits (convert Devanagari digits). Keep Nepali units as they are: "Rs 5 crore", "Rs 20 lakh",
  "3 ropani". Do not convert crore/lakh to million, and do not convert Bikram Sambat (BS) dates to AD.
- Spell Nepali names and places the standard English way (e.g. Pushpa Kamal Dahal, Lalitpur, Koshi Province).
- When a Nepali term would confuse an English reader, explain it in a few words the first time
  (e.g. "ward chair (head of the local ward)").
- "body" is the complete story as paragraphs: as complete as the original, nothing left out.
- "points" are 3 to 5 short key points. "summary" is 2 to 4 sentences.
- "headline" is a clear English news headline (no clickbait, no quotes unless the article is about a quote).
- "category": one of %s. Use "Kathmandu" for Kathmandu Valley local news; "Provinces" for local news outside
  the valley; "Diaspora" for Nepalis abroad.
- "nepal_related": false only if the story has nothing to do with Nepal or Nepalis.
- "major": true only for big national breaking news (major disasters or accidents with deaths,
  change of government, nationwide decisions, big security events). Most stories are false.
Return JSON only.""" % ", ".join(CATS)

SCHEMA = {
    "type": "OBJECT",
    "properties": {"stories": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "nepal_related": {"type": "BOOLEAN"},
            "category": {"type": "STRING", "enum": CATS},
            "headline": {"type": "STRING"},
            "points": {"type": "ARRAY", "items": {"type": "STRING"}},
            "summary": {"type": "STRING"},
            "body": {"type": "ARRAY", "items": {"type": "STRING"}},
            "major": {"type": "BOOLEAN"},
        },
        "required": ["id", "nepal_related", "category", "headline", "points", "summary", "body", "major"],
    }}},
    "required": ["stories"],
}

DEV = str.maketrans("०१२३४५६७८९", "0123456789")


def numbers(text):
    t = (text or "").translate(DEV)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return set(re.findall(r"\d{2,}(?:\.\d+)?", t))


def check(src, res):
    """Returns None if the English story is acceptable, otherwise the reason."""
    if not res.get("headline") or not isinstance(res.get("body"), list) or not res["body"]:
        return "empty"
    if not res.get("nepal_related", True):
        return None
    body = "\n".join(res["body"])
    if len(src["text"]) >= 1500 and len(body) < 600:
        return "too short"
    if len(body) < 120:
        return "too short"
    have = numbers(src["text"] + " " + src["title"])
    out = numbers(body + " " + res["headline"] + " " + res.get("summary", "") + " " + " ".join(res.get("points", [])))
    invented = sorted(n for n in out - have if n not in {"00"})
    if invented:
        return "numbers not in the article: " + ", ".join(invented[:5])
    return None


def prompt(batch):
    parts = []
    for b in batch:
        parts.append(f'<article id="{b["id"]}" source="{b["source"]}">\nTITLE: {b["title"]}\n\n{b["text"][:12000]}\n</article>')
    return ("Rewrite each article below as instructed. Return {\"stories\": [...]} with one entry per article, "
            "using the same id.\n\n" + "\n\n".join(parts))


class Writer:
    def __init__(self, state):
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))   # Google resets at Pacific midnight
        if state.get("day") != today:
            state = {"day": today, "used": {}, "blocked": {}, "models": state.get("models")}
        self.state = state
        self.key = os.environ.get("GEMINI_KEY", "")
        self.gh = os.environ.get("GITHUB_TOKEN", "")
        self.log = {"requests": 0, "stories": 0, "rejected": [], "errors": []}
        if not state.get("models") or state.get("models_checked", 0) < time.time() - 86400:
            state["models"] = self.pick_gemini_models()
            state["models_checked"] = int(time.time())
        self.chain = [("gemini", m) for m in state["models"]] + [("github", m) for m in GH_LIST]

    # ---- which Gemini models this key can use (newest Flash first)
    def pick_gemini_models(self):
        if not self.key:
            return []
        try:
            r = requests.get(f"{GEMINI}/models?pageSize=200", headers={"x-goog-api-key": self.key}, timeout=20)
            r.raise_for_status()
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception as ex:
            self.log["errors"].append(f"model list: {ex}"[:200])
            return ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]

        def ver(n):
            m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(m.group(1)) if m else 0

        bad = re.compile(r"image|tts|live|audio|embed|thinking|exp|8b|robotics|computer|native")
        flash = sorted([n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash", n)], key=ver, reverse=True)
        lite = sorted([n for n in names if re.fullmatch(r"gemini-\d+(\.\d+)?-flash-lite", n)], key=ver, reverse=True)
        prev = sorted([n for n in names if "flash" in n and "preview" in n and not bad.search(n)], key=ver, reverse=True)
        order = []
        for n in (["gemini-flash-latest"] if "gemini-flash-latest" in names else []) + flash[:2] + prev[:1] + \
                 (["gemini-flash-lite-latest"] if "gemini-flash-lite-latest" in names else []) + lite[:1]:
            if n not in order:
                order.append(n)
        return order or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

    def exhausted(self):
        return all(self.state["blocked"].get(m, 0) > time.time() for _, m in self.chain)

    def _block(self, model, seconds, why):
        self.state["blocked"][model] = int(time.time() + seconds)
        self.log["errors"].append(f"{model}: {why}"[:200])

    # ---- one request
    def _call(self, kind, model, text):
        gap = 7 if kind == "gemini" else 4              # stay under the free per-minute limits
        wait = getattr(self, "_last", 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        if kind == "gemini":
            body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": text}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 24000,
                                         "responseMimeType": "application/json", "responseSchema": SCHEMA}}
            r = requests.post(f"{GEMINI}/models/{model}:generateContent", json=body,
                              headers={"x-goog-api-key": self.key}, timeout=180)
            if r.status_code != 200:
                return r.status_code, r.text[:400], None
            j = r.json()
            cand = (j.get("candidates") or [{}])[0]
            out = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
            return 200, cand.get("finishReason"), out
        body = {"model": model, "temperature": 0.3, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM + "\nJSON shape: " + json.dumps(SCHEMA)},
                             {"role": "user", "content": text}]}
        r = requests.post(GH_MODELS, json=body, timeout=180,
                          headers={"Authorization": f"Bearer {self.gh}", "Accept": "application/json"})
        if r.status_code != 200:
            return r.status_code, r.text[:400], None
        return 200, "", r.json()["choices"][0]["message"]["content"]

    def write(self, batch):
        """Returns {id: result} for the stories that were written and passed the checks."""
        text = prompt(batch)
        results = {}
        for kind, model in self.chain:
            if self.state["blocked"].get(model, 0) > time.time():
                continue
            if kind == "gemini" and not self.key or kind == "github" and not self.gh:
                continue
            code, info, out = self._call(kind, model, text)
            self.log["requests"] += 1
            if code == 429:
                daily = re.search(r"PerDay|per day|daily|RESOURCE_EXHAUSTED.*day|UserByModelByDay", info, re.I)
                if daily:
                    self._block(model, 3 * 3600, "daily allowance used up")
                    continue
                time.sleep(35)                          # per-minute limit: wait and try this model once more
                code, info, out = self._call(kind, model, text)
                self.log["requests"] += 1
                if code == 429:
                    self._block(model, 15 * 60, "busy (per-minute limit)")
                    continue
            if code in (400, 403, 404):
                self._block(model, 24 * 3600, f"not usable: HTTP {code} {info[:150]}")
                continue
            if code != 200:
                self._block(model, 10 * 60, f"HTTP {code} {info[:150]}")
                continue
            try:
                data = json.loads(out)
            except Exception:
                self.log["errors"].append(f"{model}: reply was not JSON ({info})")
                continue
            self.state["used"][model] = self.state["used"].get(model, 0) + 1
            got = {s.get("id"): s for s in data.get("stories", []) if isinstance(s, dict)}
            missing = []
            for b in batch:
                s = got.get(b["id"])
                why = "missing" if s is None else check(b, s)
                if why:
                    self.log["rejected"].append(f"{b['source']} {b['id']} ({model}): {why}")
                    missing.append(b)
                    continue
                s["_model"] = model
                s["points"] = [p.strip() for p in s.get("points", []) if p.strip()][:5]
                s["body"] = [p.strip() for p in s["body"] if p.strip()]
                results[b["id"]] = s
                self.log["stories"] += 1
            if not missing:
                return results
            batch = missing                             # try the rest with the next model
            text = prompt(batch)
        # stories that no model could write properly are tried again later (at most 3 times)
        for b in batch:
            b["tries"] = b.get("tries", 0) + 1
            if b["tries"] >= 3:
                results[b["id"]] = {"nepal_related": False, "body": None}
        return results

    def report(self):
        return {"used_today": self.state["used"], "models": [m for _, m in self.chain],
                "paused": {m: time.strftime("%H:%M UTC", time.gmtime(t))
                           for m, t in self.state["blocked"].items() if t > time.time()},
                **self.log, "rejected": self.log["rejected"][-15:], "errors": self.log["errors"][-15:]}
