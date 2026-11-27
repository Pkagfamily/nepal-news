"""One-off check that the AI accepts the new story format and can read an official notice (2-4 requests)."""
import json, os, sys, time, base64
sys.path.insert(0, "scripts")
b = json.loads(base64.b64decode(os.environ["NEWS_SECRET"]))
os.environ["GEMINI_KEY"] = b["GEMINI_KEY"]
import ai, official
w = ai.Writer({})
story = {"id": "t1", "source": "Test", "title": "बिरगञ्ज सुख्खा बन्दरगाहमा कन्टेनर अड्किए",
         "text": "बिरगञ्ज । सर्भर बिग्रिएपछि बिरगञ्ज सुख्खा बन्दरगाहमा दुई दिनदेखि ३०० भन्दा बढी कन्टेनर अड्किएका छन् । "
                 "भन्सार कार्यालयका अनुसार साँझसम्म जाँचपास सुरु हुनेछ । कपडा आयातकर्ताहरूले ढुवानी ढिलो भएको गुनासो गरेका छन् । " * 3}
out = w.write([story])
print("STORY", json.dumps(out, ensure_ascii=False)[:1500])
print("LOG", json.dumps(w.log, ensure_ascii=False)[:800])
src = official.SOURCES[0]
items = official.list_items(src)
pick = next(((u, t) for u, t in items if official.AUCTION.search(t)), items[0])
print("NOTICE", pick)
st, why = official.explain(w, src, pick[0], pick[1], int(time.time()))
print("OFFICIAL", why, json.dumps(st, ensure_ascii=False)[:2500])
print("LOG", json.dumps(w.log, ensure_ascii=False)[:800])
