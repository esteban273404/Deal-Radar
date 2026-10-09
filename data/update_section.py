"""Refresh one section only (default: Art & Stationery) and merge it into the current page.
Usage: python3 data/update_section.py ["Section name"]"""
import json, re, sys, time
from datetime import datetime
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import fetch_deals as f

SECTION = sys.argv[1] if len(sys.argv) > 1 else "Art & Stationery"
QUERIES = {"Art & Stationery": "art_queries", "Books & Media": "media_queries", "Outdoors": "outdoor_queries",
           "Self Care": "beauty_queries", "Home & Goods": "home_queries", "Apparel": "apparel_queries", "Produce & Groceries": "produce_queries"}[SECTION]
C = f.CONFIG
raw = []
for q in C[QUERIES]:
    try:
        raw += f.fetch_slickdeals(q)
    except Exception as e:
        print("search %r failed: %s" % (q, e))
    time.sleep(1.2)
seen = {d["id"]: d for d in raw}
excl = re.compile(r"\b(" + "|".join(re.escape(w) for w in C["exclude_words"]) + r")\b", re.I)
cache = json.loads(f.THREAD_CACHE.read_text()) if f.THREAD_CACHE.exists() else {}
today = f.NOW.date().isoformat()
new = []
for d in seen.values():
    if excl.search(d["title"]) or f.TICKET_RE.search(d["title"]) or f.REFURB_RE.search(d["title"]):
        continue
    cat = f.detect_category(d["title"])
    if f.SECTION_OF.get(cat) != SECTION:
        continue
    age = (f.NOW - datetime.fromisoformat(d["posted"])).total_seconds() / 86400 if d["posted"] else None
    if age is not None and age > C["max_age_days"]:
        continue
    price, lst, pct = f.parse_prices(d)
    d.update(category=cat, store=f.detect_store(d), price=price, list_price=lst, pct_off=pct, age_days=age,
             key=f.product_key(d["title"]), refurbished=False, msrp=None, lowest_seen=False, prev_price=None,
             history_points=0, compare=[], cheapest=False)
    if d["store"] == "eBay":
        s = f.ebay_seller(d)
        if not s:
            continue
        d["official_seller"], d["marketplace"] = s, False
    c, _ = f.thread_info(d, cache, today)
    f.apply_list_price(d, c)
    if f.is_expired(d, cache, recheck=True)[0]:
        continue
    if d.get("pct_off") is not None and not (3 <= d["pct_off"] <= 95):
        d["pct_off"], d["list_price"] = None, None
    d["score"] = f.score(d)
    text = d["title"] + " " + d["desc"] + " " + d.get("body", "")[:600]
    d["code"], d["clip"] = f.find_code(text), bool(f.CLIP_RE.search(text))
    new.append(d)
f.THREAD_CACHE.write_text(json.dumps(cache))
new.sort(key=lambda x: -x["score"])
new = new[: C.get("max_deals_per_section", 60)]
for i, d in enumerate(new):
    d["top_pick"] = d["score"] >= C["top_pick_min_score"] and i < C.get("top_picks_per_section", 24)
    d["image"] = f.embed_image(d["image"])
    for k in ("body", "store_hint", "key"):
        d.pop(k, None)
    d["desc"] = re.sub(r"\s+", " ", d["desc"]).strip()[:220]

p = json.loads(f.OUT_JSON.read_text())
p["deals"] = [d for d in p["deals"] if f.SECTION_OF.get(d["category"]) != SECTION] + new
import store_logos
p["logos"] = store_logos.logos_for([d["store"] for d in p["deals"]] + [d["store"] for d in p.get("super_deals", [])])
f.OUT_JSON.write_text(json.dumps(p, ensure_ascii=False))
f.OUT_HTML.write_text(f.TEMPLATE.read_text().replace("/*__DEALS_JSON__*/null", json.dumps(p, ensure_ascii=False).replace("</", "<\\/")))
print("%s: %d deals (%d top picks)" % (SECTION, len(new), sum(d["top_pick"] for d in new)))
for d in new[:12]:
    print("  %3d %-22s %-14s %s" % (d["score"], d["category"], d["store"], d["title"][:70]))
