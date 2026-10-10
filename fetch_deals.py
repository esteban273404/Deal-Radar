#!/usr/bin/env python3
"""Deal Radar: pull game / Pokémon / electronics deals from public feeds,
score them, and build a single self-contained page (deals.html).

Usage:  python3 fetch_deals.py
Only uses the Python standard library.
"""
import base64
import store_logos
from concurrent.futures import ThreadPoolExecutor
import email.utils
import fcntl
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA_DIR = ROOT / os.environ.get("DEAL_RADAR_DATA", "data")   # "cloud" on GitHub, "data" on the Mac
IMG_CACHE = DATA_DIR / "img-cache"
HISTORY_FILE = DATA_DIR / "price-history.json"
THREAD_CACHE = DATA_DIR / "thread-prices.json"
OUT_JSON = DATA_DIR / "deals.json"
TEMPLATE = ROOT / "template.html"
OUT_HTML = ROOT / "deals.html"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
NOW = datetime.now(timezone.utc)

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}

# --------------------------------------------------------------------------- fetch

def get(url, timeout=25):
    # Use the system curl: it trusts macOS certificates, which python.org builds often don't.
    r = subprocess.run(["curl", "-sSL", "--fail", "--max-time", str(timeout), "-A", UA, url],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "curl failed")
    return r.stdout


def fetch_slickdeals(query=None, frontpage=False):
    if frontpage:
        url = ("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals"
               "&searchin=first&rss=1")
    else:
        url = ("https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&q="
               + urllib.parse.quote_plus(query))
    root = ET.fromstring(get(url))
    out = []
    for it in root.findall(".//item"):
        body = it.findtext("content:encoded", default="", namespaces=NS)
        thumbs = re.search(r"Thumb Score:\s*([+-]?\d+)", body)
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        trd = re.search(r"[?&;]trd=([^&\"]+)", body)
        pub = it.findtext("pubDate")
        out.append({
            "id": "sd-" + (it.findtext("guid") or it.findtext("link")),
            "source": "Slickdeals",
            "title": html.unescape(it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").split("?")[0],
            "desc": html.unescape(it.findtext("description") or ""),
            "body": html.unescape(re.sub(r"<[^>]+>", " ", body)),
            "thumbs": int(thumbs.group(1)) if thumbs else 0,
            "image": img.group(1) if img else None,
            "store_hint": urllib.parse.unquote_plus(html.unescape(trd.group(1))) if trd else None,
            "posted": email.utils.parsedate_to_datetime(pub).isoformat() if pub else None,
            "frontpage": frontpage,
        })
    return out


def fetch_reddit(sub):
    url = "https://www.reddit.com/r/%s/new/.rss" % sub
    data = None
    for attempt in range(4):
        try:
            data = get(url)
            break
        except Exception:
            time.sleep(20 * (attempt + 1))
    if not data:
        raise RuntimeError("rate limited")
    root = ET.fromstring(data)
    out = []
    for e in root.findall("atom:entry", NS):
        link = e.find("atom:link", NS)
        body = e.findtext("atom:content", default="", namespaces=NS)
        img = re.search(r'<img src="([^"]+)"', html.unescape(body))
        out.append({
            "id": "rd-" + (e.findtext("atom:id", namespaces=NS) or ""),
            "source": "r/" + sub,
            "title": html.unescape(e.findtext("atom:title", default="", namespaces=NS)).strip(),
            "link": link.get("href") if link is not None else "",
            "desc": "",
            "body": html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(body))),
            "thumbs": 0,
            "image": img.group(1) if img else None,
            "store_hint": None,
            "posted": e.findtext("atom:updated", namespaces=NS)
                      or e.findtext("atom:published", namespaces=NS),
            "frontpage": False,
        })
    return out

def fetch_rss(name, url):
    """A deal site's RSS feed (e.g. Hip2Save). Keeps only posts with a price in the title."""
    root = ET.fromstring(get(url))
    out = []
    for it in root.findall(".//item"):
        title = html.unescape(it.findtext("title") or "").strip()
        if not re.search(r"\$\s?\d", title):
            continue
        body = it.findtext("content:encoded", default="", namespaces=NS) or it.findtext("description") or ""
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        pub = it.findtext("pubDate")
        out.append({
            "id": "rss-" + (it.findtext("guid") or it.findtext("link") or title),
            "source": name,
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "desc": html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))[:400],
            "body": html.unescape(re.sub(r"<[^>]+>", " ", body))[:1500],
            "thumbs": 0,
            "image": img.group(1) if img else None,
            "store_hint": None,
            "posted": email.utils.parsedate_to_datetime(pub).isoformat() if pub else None,
            "frontpage": False,
        })
    return out


def fetch_pokecenter():
    """Pokémon Center drops, from Reddit posts that link to pokemoncenter.com.
    The store itself blocks scripts, so restock-monitor posts are the signal."""
    data = None
    for attempt in range(4):
        try:
            data = get("https://www.reddit.com/domain/pokemoncenter.com/new/.rss")
            break
        except Exception:
            time.sleep(15 * (attempt + 1))
    if not data:
        raise RuntimeError("rate limited")
    pc = CONFIG.get("pokecenter", {})
    skip_subs = {s.lower() for s in pc.get("skip_subs", [])}
    noise = re.compile(r"security|protection|monitor|captcha", re.I)
    out, seen = [], set()
    for e in ET.fromstring(data).findall("atom:entry", NS):
        cat = e.find("atom:category", NS)
        sub = cat.get("term") if cat is not None else ""
        body = html.unescape(e.findtext("atom:content", default="", namespaces=NS))
        m = re.search(r'href="(https?://(?:www\.)?pokemoncenter\.com[^"]*)"', body)
        link = m.group(1) if m else ""
        title = html.unescape(e.findtext("atom:title", default="", namespaces=NS)).strip()
        posted = e.findtext("atom:updated", namespaces=NS)
        # US store only: other regions use /en-ca/, /en-gb/ ...
        if (sub.lower() in skip_subs or not link or re.search(r"/(en|fr)-(?!us)\w\w\b", link)
                or noise.search(title)):
            continue
        try:
            age = (NOW - datetime.fromisoformat(posted.replace("Z", "+00:00"))).total_seconds() / 86400
        except (TypeError, ValueError):
            continue
        if age > pc.get("max_age_days", 7):
            continue
        queue = bool(re.search(r"\bqueue\b", title + " " + link, re.I))
        name = re.sub(r"^\s*(\[[^\]]*\]\s*)?pok[eé]mon ?center\s*[:\-–]\s*", "", title, flags=re.I)
        name = re.sub(r"^pok[eé]mon tcg\s*:\s*", "", name, flags=re.I).strip()
        pid = re.search(r"/product/([\w-]+?)(?:/|$|\?)", link + "/")
        key = "queue" if queue else (pid.group(1) if pid else norm_title(name))
        if key in seen:  # newest post per product wins (feed is newest first)
            continue
        seen.add(key)
        out.append({"title": "Virtual queue is up" if queue else name, "link": link,
                    "posted": posted, "kind": "Queue" if queue else "Drop", "source": "r/" + sub})
    return out[: pc.get("max_items", 12)]

# --------------------------------------------------------------------------- parse

STORES = [
    ("Best Buy", r"best ?buy"), ("Walmart", r"walmart"), ("Target", r"\btarget\b"),
    ("GameStop", r"gamestop"), ("Nintendo", r"my nintendo store|nintendo store|nintendo\.com"),
    ("LEGO", r"lego\.com|lego store"), ("Amazon", r"amazon"),
    ("Costco", r"costco"), ("Sam's Club", r"sam'?s club"), ("Pokémon Center", r"pok[eé]mon center"),
    ("Woot", r"\bwoot\b"), ("Newegg", r"newegg"), ("Micro Center", r"micro ?center"),
    ("Kohl's", r"kohl'?s"), ("Meijer", r"meijer"), ("Dell", r"\bdell\b"),
    ("B&H", r"b ?& ?h photo|bhphotovideo"), ("Adorama", r"adorama"), ("Barnes & Noble", r"barnes"),
    ("Books-A-Million", r"books-?a-?million"), ("Half Price Books", r"half price books"),
    ("Crunchyroll Store", r"crunchyroll store|store\.crunchyroll"),
    ("Sony Direct", r"playstation direct|sony direct"), ("Microsoft Store", r"microsoft store"),
    ("eBay", r"\bebay\b"), ("Macy's", r"macy'?s"), ("Staples", r"staples"),
    # art and stationery retailers
    ("Blick", r"\bblick\b|dick blick"), ("Michaels", r"michaels"), ("JetPens", r"jetpens"),
    ("Goulet Pens", r"goulet"), ("Muji", r"\bmuji\b"), ("Hobby Lobby", r"hobby lobby"), ("Paper Source", r"paper source"),
    # grocers (Albertsons family first)
    ("Albertsons", r"albertsons"), ("Safeway", r"safeway"), ("Vons", r"\bvons\b"), ("Jewel-Osco", r"jewel[- ]?osco"),
    ("Acme Markets", r"acme markets"), ("Shaw's", r"shaw'?s"), ("Whole Foods", r"whole foods"),
    ("Amazon Fresh", r"amazon fresh"), ("Sprouts", r"sprouts farmers"), ("Kroger", r"\bkroger\b"),
    # home retailers
    ("IKEA", r"\bikea\b"), ("The Container Store", r"container store|containerstore"),
    ("Home Depot", r"home ?depot"), ("Lowe's", r"\blowe'?s\b"),
    ("Nebraska Furniture Mart", r"nebraska furniture( mart)?|\bnfm\b"), ("Wayfair", r"wayfair"),
    ("Ashley", r"ashley (furniture|homestore)|ashleyfurniture"), ("Pottery Barn", r"pottery ?barn"),
    ("West Elm", r"west ?elm"), ("Crate & Barrel", r"crate ?(&|and) ?barrel"), ("Rooms To Go", r"rooms ?to ?go"),
    ("Living Spaces", r"living spaces"), ("At Home", r"\bat home (store|stores)\b|athome\.com"),
    ("Overstock", r"overstock"), ("La-Z-Boy", r"la-?z-?boy"),
    # outdoor retailers
    ("Bass Pro", r"bass pro"), ("Cabela's", r"cabela'?s"), ("Moosejaw", r"moosejaw"), ("Steep & Cheap", r"steep ?(&|and) ?cheap"),
    ("Sierra", r"sierra trading post|sierra\.com"), ("Decathlon", r"decathlon"), ("Campmor", r"campmor"),
    ("Yeti", r"yeti\.com"), ("Solo Stove", r"solostove|solo stove\.com"),
    # beauty retailers
    ("Ulta", r"\bulta\b"), ("Sephora", r"sephora"), ("Sally Beauty", r"sally beauty"),
    ("Bluemercury", r"bluemercury"), ("Dermstore", r"dermstore"), ("SkinStore", r"skinstore"),
    ("Bath & Body Works", r"bath (&|and) body works"), ("CVS", r"\bcvs\b"), ("Walgreens", r"walgreens"),
    ("FragranceNet", r"fragrancenet"), ("FragranceX", r"fragrancex"), ("Lookfantastic", r"lookfantastic"),
    # clothing and shoe retailers (multi-brand first), then brand stores
    ("Nordstrom Rack", r"nordstrom ?rack"), ("Nordstrom", r"nordstrom"), ("JCPenney", r"jc ?penney"),
    ("Zappos", r"zappos"), ("6pm", r"\b6pm\b"), ("DSW", r"\bdsw\b"), ("Foot Locker", r"foot ?locker"),
    ("Famous Footwear", r"famous footwear"), ("Finish Line", r"finish ?line"), ("REI", r"\brei\b"),
    ("Dick's", r"dick'?s sporting"), ("Academy", r"academy sports"), ("Backcountry", r"backcountry"), ("L.L.Bean", r"l\.? ?l\.? ?bean"),
    ("Zara", r"\bzara\b"), ("Rodd & Gunn", r"rodd ?(&|and) ?gunn"), ("Patagonia", r"patagonia"), ("Levi's", r"levi'?s|levi\.com"), ("American Eagle", r"american eagle|\bae\.com"),
    ("Hollister", r"hollister"), ("Abercrombie", r"abercrombie"), ("Old Navy", r"old ?navy"),
    ("Gap", r"gap\.com|gap factory|\bgap (outlet|store)"), ("Banana Republic", r"banana republic"),
    ("J.Crew", r"j\.? ?crew"), ("Uniqlo", r"uniqlo"), ("Lululemon", r"lululemon"),
    ("Nike", r"\bnike(\.com| store)\b"), ("Adidas", r"adidas(\.com| store)\b"),
    ("Under Armour", r"under armour"), ("The North Face", r"north face"), ("Columbia", r"columbia"),
    ("Carhartt", r"carhartt"), ("New Balance", r"new balance"), ("UGG", r"\bugg\b"), ("Crocs", r"\bcrocs\b"),
]
STORE_ALIASES = {"bestbuy": "Best Buy", "best buy": "Best Buy", "amazon": "Amazon",
                 "walmart": "Walmart", "target": "Target", "gamestop": "GameStop",
                 "lego": "LEGO", "nintendo": "Nintendo"}

CATEGORY_RULES = [
    ("Pokémon TCG", r"pok[eé]mon.*(tcg|trading card|booster|elite trainer|\betb\b|blister|"
                    r"collection|tin\b|bundle|build ?(&|and) ?battle|cards?\b)|"
                    r"(elite trainer box|booster bundle)"),
    ("Amiibo", r"amiibo"),
    ("LEGO", r"\blego\b"),
    ("Controllers & Accessories",
     r"(?<!dj )(?<!midi )controller|"
     r"(switch|ps5|playstation|xbox|steam deck|quest|joy-?con|nintendo).*"
     r"(\bcase\b|protector|\bgrips?\b|\bdock\b|charger|\bcover\b|\bstrap\b|\bskin\b|\bstand\b|"
     r"pouch|carry|camera|racing wheel|\bwheel\b)|"
     r"(\bcase\b|protector|\bgrips?\b|\bdock\b|charger|\bcover\b|\bstrap\b|\bskin\b|\bstand\b|"
     r"pouch|carry|camera|racing wheel|\bwheel\b).*"
     r"(switch|ps5|playstation|xbox|steam deck|quest|joy-?con)|memory card|dualsense|joy-?con|gamepad|charging (dock|station)|"
     r"microsd express|carrying case|"
     r"steering wheel|arcade stick|fight ?stick"),
    ("Consoles", r"(switch 2|switch oled|switch lite|nintendo switch)\b.*(console|system|bundle)|"
                 r"(ps5|playstation 5).*(console|pro\b|slim|digital edition|bundle)|"
                 r"xbox series [xs].*(console|bundle|\d+ ?tb)|steam deck|rog ally|legion go|"
                 r"meta quest|\bconsole\b(?! edition)"),
    ("Games", r"\((nintendo )?switch( 2)?\)|\(ps[45]\)|\(xbox[^)]*\)|"
              r"(nintendo switch|switch 2|ps5|ps4|playstation 5|xbox series x|xbox one)\b.*"
              r"\b(game|edition|deluxe)\b|"
              r"\bfor (nintendo switch|switch 2|ps5|ps4|xbox)|"
              r"\((ps5|ps4|xbox|nsw2?|switch)[^)]*/|"
              r"\s[-–]\s(nintendo switch( 2)?|ps5|ps4|xbox series x(\|s)?)\b"),
    # the bare system: "Switch 2 (Grade A Refurbished)", "Xbox Series X 1TB", "PS5 Slim"
    ("Consoles", r"(nintendo switch( 2| oled| lite)?|switch 2|ps5|playstation 5|xbox series [xs])"
                 r"(\s+[\(\[]?(oled|lite|slim|pro|digital( edition)?|model|\d+ ?tb|"
                 r"(grade [ab] |certified )?refurbished|renewed)[\)\]]?)+(?![\w-])"),
    # obvious toys first, so a Crayola playset or a toy easel isn't an art supply
    ("Toys", r"\b(toys?|playsets?|play sets?|fisher-?price|bluey|activity table|learning toy)\b"),
    # Art & Stationery before Books, Toys and Electronics (a hardcover notebook isn't a book) ("Crayola", "Cricut", a paper "notebook" isn't a laptop)
    ("Notebooks & Planners", r"\b(notebooks?(?! (pc|computer|laptop))|planners?|journals?|sketchbooks?|"
                             r"leuchtturm(1917)?|moleskine|hobonichi|midori|traveler'?s (notebook|company)|rhodia|"
                             r"clairefontaine|tomoe river|kokuyo|campus notebook|stalogy|archer (&|and) olive|"
                             r"dot grid|bullet journal|notepads?|sticky notes|post-it)\b"),
    ("Pens & Writing", r"\b(fountain pens?|gel pens?|ballpoint|rollerball|brush pens?|fineliners?|highlighters?|"
                       r"mechanical pencils?|pen sets?|\bink\b(?! ?jet)|ink cartridges? for fountain|lamy|kaweco|twsbi|"
                       r"pilot (g2|g-2|metropolitan|vanishing point|frixion|juice|precise|kakuno)|frixion|"
                       r"zebra (sarasa|mildliner|pens?)|sarasa|mildliner|uni-?ball|jetstream|kuru ?toga|"
                       r"pentel|energel|sakura (micron|pigma)|pigma micron|tombow|stabilo|platinum preppy|"
                       r"sharpie|paper mate|washi tape|stationery|stationary)\b"),
    ("Art Supplies", r"\b(art supplies|acrylic paints?|watercolou?rs?|gouache|oil paints?|paint (sets?|markers?|pens?)|"
                     r"paint ?brush(es)?|stretched canvas|canvas (panels?|boards?|pads?)|easels?|colou?red pencils?|"
                     r"pastels?|charcoal pencils?|drawing (pad|set|pencils?)|copic|posca|prismacolor|ohuhu|arteza|"
                     r"faber-castell|winsor (&|and) newton|crayola|cricut|clay|polymer clay|alcohol markers?|"
                     r"markers?(?! board)|craft supplies|embroidery|knitting|crochet|yarn)\b"),
    # Books & Media before Toys and Electronics ("Toy Story 4K Blu-ray", "anime figure")
    ("Manga & Anime", r"\b(manga|anime|light novels?|crunchyroll|funimation|studio ghibli|one piece|demon slayer|"
                      r"jujutsu kaisen|naruto|dragon ball|my hero academia|attack on titan|chainsaw man|spy x family|"
                      r"evangelion|sailor moon|gundam|gunpla|nendoroid|figma|good smile|banpresto|bandai spirits)\b"),
    ("Movies & TV", r"\b(blu-?ray(?! player)s?|4k ultra hd|uhd blu|\bdvds?\b|steelbook|criterion( collection)?|"
                    r"complete series|the complete (first|second|\w+) season)\b"),
    ("Music", r"\b(vinyl (lp|records?|albums?|edition)|on vinyl|\blp\b|record players?|turntables?|albums?|"
              r"\bcds?\b|cd box set)\b"),
    ("Books", r"\b(paperback|hardcover|hardback|novels?|cookbooks?|graphic novels?|comic books?|comics|"
              r"book (set|series|box set|collection|bundle)|boxed set|books)\b"),
    ("Toys", r"\btoys?\b|hot wheels|barbie|\bnerf\b|funko|plush|squishmallows?|action figures?|"
             r"board games?|jigsaw|\bpuzzles?\b|play-?doh|magna-?tiles|fisher-?price|melissa (&|and) doug|"
             r"playmobil|marvel legends|paw patrol|\bbluey\b|stuffed animal|ride-on|dollhouse"),
    # phone and data plans: recognized here only so main() can leave them out
    ("Phone & Data Plans", r"\b(data plans?|unlimited (talk|data|plan)|prepaid (plan|phone|wireless)|wireless plans?|"
                           r"phone plans?|cell plans?|e-?sim|sim (card|kit)|mint mobile|visible (wireless|mobile|\+|plus)|"
                           r"us mobile|total wireless|tracfone|google fi|cricket wireless|metro by t-mobile|"
                           r"straight talk|boost (mobile|infinite)|\btello\b|xfinity mobile|spectrum mobile|"
                           r"t-mobile prepaid|at&t prepaid|verizon prepaid|\d+ ?gb (of )?(high-speed )?data)\b|\bvisible\+"),
    ("Electronics", r"\b(4k|oled|qled|mini-led)\b.*\btv\b|\btelevision\b|\bmonitor\b|"
                    r"headphones?|earbuds|airpods|soundbar|\bssd\b|\bnvme\b|laptop|macbook|"
                    r"\bipad\b|tablet|graphics card|\brtx \d|\bgpu\b|router|smartwatch|"
                    r"bluetooth speaker|projector|webcam|smartphones?|unlocked|iphone \d+|galaxy [sza]\d+|"
                    r"pixel \d+[a-z]?\b|oneplus \d+|moto g\b|notebook pc|apple pencil|stylus"),
    # Outdoors before Self Care, Home & Goods and apparel ("life jacket", "North Face tent", "camping blanket")
    ("Fishing", r"\b(fishing|rod (and|&) reel|spinning reel|baitcast\w*|tackle (box|bag)|fishing lures?|"
                r"ugly stik|penn (battle|spinfisher)|shimano (reel|sienna|stradic))\b"),
    ("Bikes, Water & Snow", r"\b(bikes?|bicycles?|e-?bikes?|cycling|bike (helmets?|locks?|lights?|racks?|pumps?)|"
                            r"helmets?|kayaks?|paddle ?boards?|sup board|canoes?|life (jackets?|vests?)|pfds?|"
                            r"snorkel\w*|wetsuits?|skis|skiing|snowboards?|ski goggles|snowshoes?|sleds?)\b"),
    ("Camping & Hiking", r"\b(tents?|camping|campsite|sleeping (bags?|pads?)|hammocks?|trekking poles|"
                         r"hiking (backpacks?|packs?|poles)|backpacking|daypacks?|camp (chairs?|stoves?|kitchen|cots?)|"
                         r"camping stoves?|jetboil|lanterns?|headlamps?|water filters?|lifestraw|bear spray|tarps?|"
                         r"osprey|black diamond|big agnes|nemo equipment|kelty|coleman|therm-a-rest)\b"),
    ("Outdoor Gear", r"\b(coolers?|yeti|rtic|igloo|binoculars|rangefinders?|multi-?tools?|pocket ?knife|"
                     r"leatherman|flashlights?|fire ?pits?|solo stove|grills?|griddles?|blackstone|traeger|"
                     r"pellet smoker|smokers?|golf (balls?|clubs?|bags?)|disc golf|pickleball|camp chairs?)\b"),
    ("Tools & Garden", r"\b(drills?|impact (driver|wrench)|drivers? kit|(circular|miter|table|jig|reciprocating|oscillating|chain) ?saws?|"
                       r"chainsaws?|sanders?|(?<!coffee )(?<!spice )(?<!burr )(?<!herb )(?<!meat )grinders?|angle grinder|router kit|nail gun|tool (set|kit|box|chest|bag)|toolbox|"
                       r"socket set|wrench(es| set)?|screwdrivers?|pliers|tape measure|stud finder|work ?bench|shop vac|"
                       r"wet/dry vac|ladders?|lawn ?mowers?|leaf blowers?|string trimmers?|hedge trimmers?|weed eater|"
                       r"pressure washers?|garden hose|hose reel|wheelbarrow|snow blowers?|generators?|"
                       r"dewalt|milwaukee|ryobi|makita|ridgid|craftsman|kobalt|husky|ego power|greenworks|"
                       r"bosch (drill|tool)|black ?\+? ?decker|worx|toro|stihl)\b"),
    # Self Care and Home & Goods come before apparel, so a "fleece throw blanket" isn't a jacket
    ("Hair Tools", r"hair ?dryer|blow ?dryer|flat ?iron|hair straightener|curling (iron|wand)|airwrap|"
                   r"supersonic|hot (air )?brush|\bairstrait\b"),
    ("Makeup", r"\b(makeup|make-up|cosmetics?|mascara|eyeliner|eye ?shadow|eyeshadow palette|lipstick|"
               r"lip ?gloss|lip (oil|liner|stain|tint)|concealer|bronzer|blush|setting (spray|powder)|"
               r"face primer|brow (gel|pencil)|false lashes|nail polish|press-on nails|makeup brush(es)?|"
               r"maybelline|nyx|e\.l\.f\.|\belf cosmetics|revlon|covergirl|fenty beauty|rare beauty|too faced|"
               r"urban decay|benefit cosmetics|nars|mac cosmetics|charlotte tilbury|huda beauty|tarte|"
               r"anastasia beverly hills|milani)\b"),
    ("Personal Care", r"\b(shampoos?|conditioners?|body ?wash|shower gel|bar soap|hand soap|soap bars?|"
                      r"deodorants?|antiperspirant|toothpaste|toothbrush(es)?|mouthwash|dental floss|"
                      r"floss picks|whitening strips|razors?|razor blades|shaving (cream|gel)|shavers?|"
                      r"(beard|hair|body|nose) trimmers?|cotton swabs|q-tips|tampons|maxi pads|diapers|baby wipes|hair gel|dry shampoo|"
                      r"hand sanitizers?|pocketbac|vitamins?|multivitamins?|supplements?|probiotics?|melatonin|"
                      r"fish oil|omega-3|head ?& ?shoulders|dove men|old spice|gillette|oral-b|sonicare|crest|colgate|"
                      r"native deodorant|dr\.? squatch)\b"),
    ("Household", r"\b(laundry|detergent|tide pods|fabric softener|dryer sheets|dish ?soap|dishwasher "
                  r"(pods|detergent|tabs)|cascade|paper towels?|toilet paper|bath tissue|facial tissues?|"
                  r"kleenex|trash bags|garbage bags|cleaning (supplies|wipes|spray)|disinfecting wipes|"
                  r"lysol|clorox|bleach|sponges|swiffer|mop|air freshener|febreze|ziploc|aluminum foil|"
                  r"plastic wrap|food storage|storage bags|zipper bags|sandwich bags|light bulbs|batteries|blankets?|throws?|comforters?|duvets?|bed ?sheets|"
                  r"sheet sets?|pillows?|bath towels?|towel sets?|rugs?|curtains?|quilts?)\b"),
    ("Home & Goods", r"\b(furniture|bookcases?|bookshelf|bookshelves|shelving|shelf|shelves|storage (bins?|boxes?|"
                     r"containers?|cabinets?|shelf|racks?|carts?|ottoman|bench|baskets?|drawers?|units?)|organizers?|"
                     r"closet (system|organizer)|elfa|drawer (organizers?|units?|dividers?)|bins|baskets|"
                     r"dressers?|nightstands?|desks?|office chairs?|dining (table|chairs?)|coffee table|sofas?|"
                     r"couch(es)?|sectional|mattress(es)?|bed frames?|lamps?|floor lamp|mirrors?|wall art|"
                     r"kallax|billy bookcase|malm|hemnes|alex drawer|lack table|vacuums?|robot vacuum|"
                     r"air purifiers?|air fryers?|coffee makers?|blenders?|cookware|knife sets?|dinnerware)\b"),
    ("Skincare", r"\b(skin ?care|moisturi[sz]ers?|moisturizing (cream|lotion)|face (wash|cream|lotion|mask|oil)|"
                 r"facial (cleanser|toner|serum)|cleansers?|serums?|retinol|niacinamide|hyaluronic|"
                 r"sunscreen|spf ?\d+|eye cream|toner pads|sheet masks?|micellar|exfoliat\w+|body lotion|"
                 r"body butter|lip balm|cerave|la roche-? ?posay|neutrogena|olay|aveeno|cetaphil|"
                 r"the ordinary|paula'?s choice|drunk elephant|tatcha|clinique|est[eé]e lauder|lanc[oô]me|"
                 r"kiehl'?s|first aid beauty|cosrx|laneige|olaplex|k18|lotion)\b"),
    ("Fragrance", r"\b(perfumes?|colognes?|eau de (parfum|toilette|cologne)|edp|edt|body mists?|"
                  r"fine fragrance mists?|fragrance(?![- ]free)s?|3-wick|candles?|wallflowers?|room sprays?)\b"),
    # Apparel. Specific garment words first; brand names alone fall through to Clothing.
    ("Shoes & Boots", r"\b(shoes?|sneakers?|boots?|sandals?|slides|slippers?|cleats|loafers|clogs|"
                      r"mules|trainers|hiking shoes?|running shoes?|birkenstocks?|dr\.? martens|hoka|"
                      r"skechers|\bcrocs\b|\bvans\b|converse|chuck taylor|air max|air force 1|jordan \d+|ultraboost|"
                      r"samba|gazelle|spezial|stan smith|campus 00s|pegasus|cloudmonster|gel-\w+)\b"),
    ("Jackets & Outerwear", r"\b(jackets?|coats?|parkas?|puffers?|fleece|vests?|windbreakers?|anoraks?|"
                            r"rain ?shell|softshell|nano puff|down sweater|better sweater)\b"),
    ("Hats & Accessories", r"\b(hats?|caps?|beanies?|gloves|mittens|scarf|scarves|belts?|wallets?|"
                           r"sunglasses|socks|backpacks?|duffel|tote bag)\b"),
    ("Clothing", r"\b(shirts?|t-?shirts?|tees?|polos?|shorts|pants|jeans|joggers|leggings|hoodies?|"
                 r"sweatshirts?|sweaters?|cardigans?|flannels?|chinos|dress(es)?|skirts?|underwear|boxers?|"
                 r"briefs|bras?|pajamas|swim(wear|suit|suits)?|apparel|clothing|outfits?|activewear|"
                 r"levi'?s|patagonia|american eagle|hollister|abercrombie|old navy|gap factory|banana republic|"
                 r"j\.? ?crew|uniqlo|lululemon|under armour|north face|carhartt|columbia|nike|adidas|"
                 r"new balance|\bugg\b|timberland|puma|reebok|rodd ?(&|and) ?gunn)\b"),
    # fresh produce, last ("apples"/"bananas" plural only, so Apple and Banana Republic stay out)
    ("Produce", r"\b(fresh produce|produce|fruits?(?! of the loom| snacks?| punch| loops)|vegetables?|veggies|"
                r"avocados?(?! (green )?mattress)|bananas|apples|strawberr(y|ies)|blueberr(y|ies)|raspberr(y|ies)|"
                r"blackberr(y|ies)|grapes|oranges|clementines|mandarins|lemons|limes|peaches|pears|plums|cherries|"
                r"mangoes|mangos|pineapples?|watermelons?|cantaloupe|kiwis?|tomatoes|potatoes|sweet potatoes|"
                r"onions|garlic bulbs|lettuce|spinach|kale|broccoli|cauliflower|carrots|celery|bell peppers|"
                r"cucumbers|zucchini|squash|mushrooms|salad kits?|bagged salad)\b"),
]

PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)(?![\d,.]*\d)(?!\+)(?!\s*off)", re.I)
LIST_RE = re.compile(
    r"(?:list(?: price)?|reg(?:ular(?:ly)?)?\.?(?: price)?|orig(?:inally|inal(?: price)?)?\.?|"
    r"\bwas\b|msrp|retails? (?:for|at)?)\s*:?\s*\$\s?(\d[\d,]*(?:\.\d{1,2})?)", re.I)
PCT_RE = re.compile(r"(\d{1,2})\s?%\s?off", re.I)


DIGITAL_STORES = (r"nintendo\W+co\W*(wbr\W+)?m?\W+us\W+s(tore)?\b|store/products|"
                  r"store\.playstation|xbox\.com\S*/games/store|store\.steampowered")


# Controllers: only first-party (Nintendo, Sony, Microsoft).
CONTROLLER_RE = re.compile(r"controller|gamepad|joy-?con|dualsense|dualshock", re.I)
FIRST_PARTY_RE = re.compile(
    r"(nintendo|switch( 2)?|switch 2) pro controller|joy-?con|dualsense|dualshock|"
    r"(sony|playstation|ps5).{0,40}controller|"
    r"(microsoft|xbox).{0,40}(wireless|elite|adaptive|core).{0,30}controller|xbox elite|xbox adaptive", re.I)
THIRD_PARTY_BRANDS = re.compile(
    r"\b(powera|8bitdo|gamesir|hyperkin|pdp|turtle beach|razer|nyko|hori|thrustmaster|scuf|victrix|"
    r"nacon|data ?frog|binbok|easysmx|backbone|kishi|rotor riot|afterglow|snakebyte|mobapad|luna|"
    r"gulikit|flydigi|machenike|bigbig|ipega|voyee|fantech|logitech|steelseries|beboncool|"
    r"rock candy|retro-?bit|retroid|ayaneo)\b", re.I)
# Product names only the platform owner uses; "for PS5" phrasing is fine with these.
OFFICIAL_NAMES = re.compile(r"dualsense|dualshock|joy-?con|(nintendo )?switch( 2)? pro controller|"
                            r"xbox elite|xbox adaptive|xbox wireless controller", re.I)
THIRD_PARTY_PHRASING = re.compile(r"\bfor (nintendo )?(switch|xbox|ps[45]|playstation)|compatible with|"
                                  r"\bfor (pc|iphone|android)", re.I)


def is_first_party_controller(title):
    if THIRD_PARTY_BRANDS.search(title):
        return False
    if OFFICIAL_NAMES.search(title):
        return True
    return bool(FIRST_PARTY_RE.search(title)) and not THIRD_PARTY_PHRASING.search(title)


REFURB_RE = re.compile(r"\b(refurbished|refurb|renewed|certified refurbished)\b", re.I)


# No headphones / headsets, except Apple's own (AirPods, Beats, anything Apple).
AUDIO_RE = re.compile(r"headphones?|headsets?|earbuds?|earphones?|ear ?buds|\bin-ear\b|over-ear|on-ear|open[- ]ear|\biems?\b", re.I)
APPLE_RE = re.compile(r"\b(apple|airpods|beats)\b", re.I)


SWITCH2_RE = re.compile(r"switch 2|\bns ?w?2\b", re.I)
KEYCARD_RE = re.compile(r"game[- ]?key[- ]?card|\bkey[- ]?card\b", re.I)


def is_switch2_game(d):
    return d["category"] == "Games" and bool(SWITCH2_RE.search(d["title"]))


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def money(s):
    return float(s.replace(",", ""))


DOMAINS = {"bestbuy": "Best Buy", "walmart": "Walmart", "target": "Target", "gamestop": "GameStop",
           "nintendo": "Nintendo", "lego": "LEGO", "amazon": "Amazon", "costco": "Costco",
           "samsclub": "Sam's Club", "pokemoncenter": "Pokémon Center", "woot": "Woot",
           "newegg": "Newegg", "microcenter": "Micro Center", "staples": "Staples", "ebay": "eBay",
           "aliexpress": "AliExpress", "samsung": "Samsung", "dell": "Dell", "qvc": "QVC",
           "kohls": "Kohl's", "bhphotovideo": "B&H", "adorama": "Adorama", "meijer": "Meijer",
           "playstation": "PlayStation Direct", "microsoft": "Microsoft Store", "lenovo": "Lenovo",
           "hp": "HP", "apple": "Apple",
           "macys": "Macy's", "nordstromrack": "Nordstrom Rack", "nordstrom": "Nordstrom",
           "jcpenney": "JCPenney", "zappos": "Zappos", "dsw": "DSW", "footlocker": "Foot Locker",
           "famousfootwear": "Famous Footwear", "finishline": "Finish Line", "rei": "REI",
           "dickssportinggoods": "Dick's", "backcountry": "Backcountry", "llbean": "L.L.Bean",
           "patagonia": "Patagonia", "levi": "Levi's", "levis": "Levi's", "ae": "American Eagle",
           "americaneagle": "American Eagle", "hollisterco": "Hollister", "hollister": "Hollister",
           "abercrombie": "Abercrombie", "oldnavy": "Old Navy", "gap": "Gap", "gapfactory": "Gap",
           "bananarepublic": "Banana Republic", "bananarepublicfactory": "Banana Republic", "jcrew": "J.Crew", "uniqlo": "Uniqlo",
           "ulta": "Ulta", "sephora": "Sephora", "sallybeauty": "Sally Beauty", "bluemercury": "Bluemercury",
           "dermstore": "Dermstore", "skinstore": "SkinStore", "bathandbodyworks": "Bath & Body Works",
           "cvs": "CVS", "walgreens": "Walgreens", "fragrancenet": "FragranceNet", "fragrancex": "FragranceX",
           "lookfantastic": "Lookfantastic", "clinique": "Clinique", "esteelauder": "Estée Lauder",
           "lancome": "Lancôme", "elfcosmetics": "e.l.f.",
           "dickblick": "Blick", "blick": "Blick", "michaels": "Michaels", "jetpens": "JetPens",
           "gouletpens": "Goulet Pens", "muji": "Muji", "hobbylobby": "Hobby Lobby", "papersource": "Paper Source",
           "albertsons": "Albertsons", "safeway": "Safeway", "vons": "Vons", "jewelosco": "Jewel-Osco",
           "acmemarkets": "Acme Markets", "shaws": "Shaw's", "wholefoodsmarket": "Whole Foods",
           "sprouts": "Sprouts", "kroger": "Kroger",
           "ikea": "IKEA", "containerstore": "The Container Store",
           "homedepot": "Home Depot", "lowes": "Lowe's",
           "nfm": "Nebraska Furniture Mart", "wayfair": "Wayfair", "ashleyfurniture": "Ashley", "ashley": "Ashley",
           "potterybarn": "Pottery Barn", "westelm": "West Elm", "crateandbarrel": "Crate & Barrel",
           "roomstogo": "Rooms To Go", "livingspaces": "Living Spaces", "athome": "At Home",
           "overstock": "Overstock", "lazboy": "La-Z-Boy",
           "basspro": "Bass Pro", "cabelas": "Cabela's", "moosejaw": "Moosejaw", "steepandcheap": "Steep & Cheap",
           "sierra": "Sierra", "decathlon": "Decathlon", "campmor": "Campmor", "yeti": "Yeti", "solostove": "Solo Stove",
           "roddandgunn": "Rodd & Gunn", "booksamillion": "Books-A-Million", "barnesandnoble": "Barnes & Noble",
           "hpb": "Half Price Books", "crunchyroll": "Crunchyroll Store", "academy": "Academy", "zara": "Zara", "jcrewfactory": "J.Crew", "amaozn": "Amazon", "amzon": "Amazon", "lululemon": "Lululemon", "nike": "Nike", "adidas": "Adidas",
           "underarmour": "Under Armour", "thenorthface": "The North Face", "columbia": "Columbia",
           "carhartt": "Carhartt", "newbalance": "New Balance", "ugg": "UGG", "crocs": "Crocs"}


FOOD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in CONFIG.get("food_words", [])) + r")\b", re.I) \
    if CONFIG.get("food_words") else None
HOME_CATS = {"Personal Care", "Household", "Home & Goods"}
# not food even if a food word appears: "coconut milk shampoo", "rice cooker", "coffee maker"
NOT_FOOD_RE = re.compile(r"\b(maker|cooker|machine|grinder|blender|fryer|toaster|kettle(?! brand)|oven|pans?|pots?|"
                         r"containers?|storage|dispenser|scale|mug|tumbler|frother|press|scented|candle|"
                         r"shampoo|conditioner|lotion|soap|wash|scrub|mattress|pillows?|sheets|topper|sofa|rug|food[- ]grade|"
                         r"spatulas?|utensils?|silicone|warmers?|bowl sets?|mixing bowls?|plates?|lunch ?box|bento|water bottles?|"
                         r"vitamins?|multivitamin|supplements?)\b", re.I)


def is_food(title):
    plain = re.sub(r"\([^)]*\)", " ", title)   # "(Avocado)" is usually a color
    return bool(FOOD_RE and FOOD_RE.search(plain) and not NOT_FOOD_RE.search(plain))


SECTION_OF = {}
for _sec, _cats in {
        "Toys & Games": ["Pokémon TCG", "Consoles", "Games", "Controllers & Accessories", "Amiibo", "LEGO", "Toys"],
        "Electronics": ["Electronics"],
        "Books & Media": ["Books", "Manga & Anime", "Movies & TV", "Music"],
        "Art & Stationery": ["Art Supplies", "Notebooks & Planners", "Pens & Writing"],
        "Apparel": ["Shoes & Boots", "Clothing", "Jackets & Outerwear", "Hats & Accessories"],
        "Outdoors": ["Camping & Hiking", "Bikes, Water & Snow", "Fishing", "Outdoor Gear"],
        "Self Care": ["Makeup", "Skincare", "Personal Care", "Fragrance", "Hair Tools"],
        "Home & Goods": ["Household", "Tools & Garden", "Home & Goods"],
        "Produce & Groceries": ["Produce", "Groceries"]}.items():
    for _c in _cats:
        SECTION_OF[_c] = _sec
# event, movie, park and sports tickets are left out (the board game "Ticket to Ride" isn't a ticket)
TICKET_RE = re.compile(r"\btickets?\b(?! to ride)|\bday pass(es)?\b|\bsingle-day\b", re.I)
def ebay_seller(d):
    """Who sells an eBay deal, from Slickdeals' wording ("adidas via eBay", "eBay seller adidas", "Target eBay store").
    Returns the official seller's name, or None for a third-party or unknown seller."""
    text = re.sub(r"\[[^\]]*\]", " ", d.get("desc", "") + " " + d.get("body", "")[:400])
    text = re.sub(r"\s+", " ", text)
    pats = [r"([\w&'.\- ]{2,40}?)(?: \([\w ]+\))? via ebay", r"ebay seller ([\w&'.\-]+(?: [\w&'.\-]+){0,2})",
            r"([\w&'.\- ]{2,30}?) ebay store", r"ebay ([\w&'.\- ]{2,30}?) store", r"^\W*(ebay) has\b"]
    official = {x.lower() for x in CONFIG.get("ebay_official_sellers", [])}
    pretty = {"bullseyedeals": "Target", "bullseye deals": "Target", "target": "Target", "best buy": "Best Buy", "ebay": "eBay", "hp": "HP", "lg": "LG"}
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            words = m.group(1).strip().split()
            # any run of up to 4 words in the match: "Joe's New Balance Outlet", "adidas has ..." -> "adidas"
            for n in range(min(4, len(words)), 0, -1):
                for a in range(len(words) - n + 1):
                    name = " ".join(words[a:a + n])
                    if name.lower() in official:
                        return pretty.get(name.lower(), name)
    return None


APPAREL_CATS = {"Shoes & Boots", "Clothing", "Jackets & Outerwear", "Hats & Accessories"}
JUNK_HINTS = {"men", "women", "mens", "womens", "kids", "more", "default", "deal", "sale", "other", "store", "online", "terms and conditions", "terms"}


def detect_store(d):
    hint = re.sub(r"\bb\b", " ", (d.get("store_hint") or "")).strip()
    if hint.lower() in JUNK_HINTS:
        hint = ""
    if hint:
        via = re.search(r"via (\w+)", hint, re.I)
        for w in re.findall(r"[a-z]+", (via.group(1) if via else hint).lower()):
            if w in DOMAINS:
                if via:
                    d["marketplace"] = True
                return DOMAINS[w]
        if "http" not in hint and len(hint.split()) <= 3 and not re.search(r"\d", hint):
            via = re.search(r"via (\w+)", hint, re.I)
            if via:
                d["marketplace"] = True
                hint = via.group(1)
            hint = re.sub(r"\s+", " ", hint).strip()
            return STORE_ALIASES.get(hint.lower(), DOMAINS.get(hint.lower().replace(" ", ""), hint))
    m = re.search(r"\bat ([A-Z][\w&'. ]{2,25})\s*$", d["title"])
    text = (m.group(1) if m else "") + " " + d["title"] + " " + d["desc"][:200]
    for name, pat in STORES:
        if re.search(pat, text, re.I):
            return name
    return "Other"


# processed food and non-food that mention a fruit or vegetable: "avocado oil chips", "raspberry hand soap",
# hand warmers in the color "(Avocado)"
NOT_PRODUCE_RE = re.compile(r"\b(chips|oils?|bars?|snacks?|gumm(y|ies)|vitamins?|multivitamin|juices?|sauces?|dried|"
                            r"powder|candy|jam|jelly|spread|dressing|soaps?|toothpaste|lotion|perfume|warmers?|"
                            r"scent(ed)?|flavou?r(ed|s)?|cereal|yogurt|smoothie|drinks?|soda|seeds?|plants?|"
                            r"seasoning|canned|frozen|puree|baby food|pouch(es)?)\b", re.I)


def detect_category(title):
    for name, pat in CATEGORY_RULES:
        if name == "Produce":
            plain = re.sub(r"\([^)]*\)", " ", title)   # "(Avocado)" is usually a color
            if re.search(pat, plain, re.I) and not NOT_PRODUCE_RE.search(plain):
                return name
            continue
        if re.search(pat, title, re.I):
            return name
    if is_food(title):
        return "Groceries"   # packaged food and drink, checked after everything else
    return None


def parse_prices(d):
    m = PRICE_RE.search(d["title"])
    price = money(m.group(1)) if m else None
    if price is None and re.search(r"\bfree\b", d["title"], re.I):
        price = 0.0
    lst = None
    for text in (d["title"], d["desc"], d["body"]):
        lm = LIST_RE.search(text)
        if lm:
            lst = money(lm.group(1))
            break
    pct = None
    if price is not None and lst and lst > price > 0:
        pct = round((1 - price / lst) * 100)
    else:
        pm = PCT_RE.search(d["title"])
        if pm:
            pct = int(pm.group(1))
            if price:
                lst = round(price / (1 - pct / 100), 2)
    if pct is not None and not (3 <= pct <= 95):
        pct, lst = None, None
    return price, lst, pct


def product_key(title):
    t = title.lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*shipping[^)]*\)", " ", t)
    t = PRICE_RE.sub(" ", t)
    t = re.sub(r"\b(at|from|for|the|and|with|w/|free|shipping|prime|ymmv|in-store|"
               r"online|deal|sale|off|new|\d+%)\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    words = sorted(set(w for w in t.split() if len(w) > 1))
    return " ".join(words)


def msrp_check(d):
    if d["category"] != "Pokémon TCG" or d["price"] is None:
        return None
    t = d["title"].lower().replace("pokémon", "pokemon")
    best = None
    for name, val in CONFIG["pokemon_msrp"].items():
        if name in t and (best is None or len(name) > len(best[0])):
            best = (name, val)
    if not best:
        return None
    return {"product": best[0], "msrp": best[1]}

# --------------------------------------------------------------------------- score

def score(d):
    s = 0.0
    s += min(40, 10 * math.log2(1 + max(d["thumbs"], 0)))
    if d["pct_off"]:
        s += min(20, d["pct_off"] * 0.3)
    age = d["age_days"] if d["age_days"] is not None else 5
    s += 15 * max(0.0, 1 - age / 7)
    if d["frontpage"]:
        s += 10
    if d["source"].startswith("r/") or d["source"] in CONFIG.get("rss_feeds", {}):
        s += CONFIG.get("community_base_score", 30)  # no vote counts in Reddit or deal-site feeds
    m = d.get("msrp")
    if m and d["price"] is not None:
        if d["price"] <= m["msrp"] * 1.001:
            s += 12
        elif d["price"] > m["msrp"] * 1.15:
            s -= 20
    if d.get("lowest_seen"):
        s += 8
    if d["thumbs"] < 0:
        s -= 15
    return max(0, min(100, round(s)))

# --------------------------------------------------------------------------- images

def shrink_image(src, dst):
    """Resize to fit 180px as a JPEG: macOS's built-in sips on the Mac, Pillow in the cloud (GitHub)."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["sips", "-Z", "180", "-s", "format", "jpeg", "-s", "formatOptions", "70",
                                str(src), "--out", str(dst)], capture_output=True)
            return r.returncode == 0 and dst.exists()
        from PIL import Image
        with Image.open(src) as im:
            im.thumbnail((180, 180))
            bg = Image.new("RGB", im.size, (255, 255, 255))   # transparent PNGs get a white background
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            bg.save(dst, "JPEG", quality=70)
        return True
    except Exception:
        return False


def embed_image(url):
    if not url:
        return None
    IMG_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()
    path = IMG_CACHE / key
    try:
        if path.exists():
            raw = path.read_bytes()
        else:
            src = path.with_suffix(".src")
            src.write_bytes(get(url, timeout=15))
            if not shrink_image(src, path):
                path.write_bytes(src.read_bytes())
            src.unlink()
            raw = path.read_bytes()
            if len(raw) > 150_000:
                return None
    except Exception:
        return None
    mime = "image/png" if raw[:4] == b"\x89PNG" else (
        "image/webp" if raw[8:12] == b"WEBP" else (
            "image/gif" if raw[:3] == b"GIF" else "image/jpeg"))
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())

# --------------------------------------------------------------------------- main

def thread_info(d, cache, today):
    """Regular price / final price / key-card mention from a Slickdeals thread page (cached).
    Returns (info or None, fetched_now)."""
    m = re.search(r"/f/(\d+)", d["link"])
    if d["source"] != "Slickdeals" or not m:
        return None, False
    tid = m.group(1)
    fetched = False
    if tid not in cache or (is_switch2_game(d) and "keycard" not in cache[tid]):
        try:
            page = get(d["link"], timeout=20).decode("utf-8", "replace")
            lp = re.search(r'"listPrice":"?([\d.]+)', page)
            fp = re.search(r'"finalPrice":"?([\d.]+)', page)
            ex = re.search(r'"isExpiredDeal":"(Yes|No)"', page)
            cache[tid] = {"list": float(lp.group(1)) if lp else None,
                          "final": float(fp.group(1)) if fp else None, "date": today,
                          "keycard": bool(KEYCARD_RE.search(page)),
                          "expired": ex is not None and ex.group(1) == "Yes", "checked": NOW.isoformat()}
        except Exception as e:
            print("thread %s failed: %s" % (tid, e))
            cache[tid] = {"list": None, "final": None, "date": today}
        fetched = True
        time.sleep(0.8)
    return cache[tid], fetched


EXPIRY_RECHECK_HOURS = 8


def is_expired(d, cache, recheck):
    """True if Slickdeals marks the deal expired. With recheck, re-reads the page when the
    last check is older than EXPIRY_RECHECK_HOURS. Returns (expired, fetched_now)."""
    m = re.search(r"/f/(\d+)", d["link"])
    if d["source"] != "Slickdeals" or not m:
        return False, False
    c = cache.get(m.group(1)) or {}
    checked = c.get("checked")
    fresh = checked and (NOW - datetime.fromisoformat(checked)).total_seconds() < EXPIRY_RECHECK_HOURS * 3600
    if not recheck or fresh:
        return bool(c.get("expired")), False
    try:
        page = get(d["link"], timeout=20).decode("utf-8", "replace")
        ex = re.search(r'"isExpiredDeal":"(Yes|No)"', page)
        c = dict(c, expired=ex is not None and ex.group(1) == "Yes", checked=NOW.isoformat())
        c.setdefault("date", NOW.date().isoformat())
        c.setdefault("list", None); c.setdefault("final", None)
        cache[m.group(1)] = c
    except Exception as e:
        print("expiry check %s failed: %s" % (m.group(1), e))
    time.sleep(0.6)
    return bool(c.get("expired")), True


# --------------------------------------------------------------------------- coupon codes

CODE_RE = re.compile(r"(?i:(?:promo|coupon|discount|offer|checkout|use|with|apply|enter)?\s*(?:code|coupon)s?)\s*[:\-]?\s*"
                     r"[\"'“*]?([A-Z0-9][A-Z0-9\-]{3,24})\b")
NOT_CODES = {"CODE", "FREE", "SHIPPING", "PRIME", "AMAZON", "NONE", "BELOW", "OFF", "SAVE", "DEAL", "PRICE",
             "CART", "ITEM", "ITEMS", "NEEDED", "REQUIRED", "APPLIED", "AUTO", "AUTOMATICALLY", "HERE", "PAGE",
             "VIA", "WITH", "THE", "AND", "FOR", "USA", "NEW", "SALE", "LINK"}
CLIP_RE = re.compile(r"clip(pable|ped)?\s+(the\s+)?(\$\d+(\.\d+)?|\d+%)?\s*(off\s+)?coupon|clippable|"
                     r"coupon on (the )?(product )?page|on-?page coupon|check the coupon box", re.I)


def clean_title(t):
    """A readable product name for the card. Price, store, shipping and coupon details are already shown
    elsewhere on the card, so forum shorthand like "[SnS, AC] $4.96* | ... at Amazon + Free S&H" goes."""
    orig = t
    t = re.sub(r"\s+", " ", t).strip()
    for _ in range(3):   # leading tags can stack: "[Prime] YMMV: Prime Members: ..."
        t = re.sub(r"^\s*(\[[^\]]{1,30}\]|\((?:YMMV|YYMV|AC|S&S|SnS)\??\)|(?:YMMV|YYMV|Prime Members(?: in Select Locations)?|"
                   r"Prime Day|Prime Big Deal Days|Prime|Lightning Deal|Today Only|Back in stock|HOT|NEW)\s*[:\-–!]\s*)\s*", "", t, flags=re.I)
    t = re.sub(r"^\$[\d,.]+\*?\s*(\(.*?\))?\s*[|:\-–]\s*", "", t)                 # "$4.96* | ..."
    t = re.sub(r"^\d+/\$[\d.]+\s+", "", t)                                             # "3/$7.71 ..."
    t = re.sub(r"\s+(?:at|@|on|from|via)\s+(?:Amazon(?:\.com)?|Walmart(?:\.com)?|Target|Best ?Buy|Woot!?|eBay|"
               r"Costco|Sam'?s Club|Kohl'?s|Macy'?s|Newegg|GameStop|Amazon Grocery)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*(?:\+|w/|with|&)\s*(?:free\s+(?:prime\s+)?(?:s&h|shipping|ship|store pickup)|fs)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:w/|with)\s+(?:prime|s&s|subscribe ?(?:&|and) ?save|walmart\+|coupon|code|amex)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:just|only|now|for|from|starting at|as low as|under)?\s*\$[\d,.]+\*?(?:\s*(?:each|ea|/mo|/lb|/ea|"
               r"shipped|after .*|or less|\(.*?\)|\+.*|wyb.*|w/.*))*\s*$", "", t, flags=re.I)   # trailing price
    t = re.sub(r"\s*\((?:Reg\.?|Regularly|was)\s*\$[\d.,]+\)", "", t, flags=re.I)
    t = re.sub(r"\s*\|\s*(?:prices|styles|deals|items)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:for|from|just|only|now|at|on)\s*$", "", t, flags=re.I)        # dangling "for"
    t = re.sub(r"\s*[|–-]\s*$", "", t).strip(" ,;:|-–")
    return t if len(t) >= 12 else orig


def is_name_word(code, text):
    """True if the code also appears in the title as an ordinary word (not right after "code"/"coupon")."""
    title = text[:200]
    title = re.sub(r"(?i)(?:code|coupon)s?\s*[:\-]?\s*[\"'“*]?" + re.escape(code), " ", title)
    return re.search(r"\b%s\b" % re.escape(code), title, re.I) is not None


def find_code(text):
    """A checkout code mentioned in the deal text ("promo code SAVE20", "after coupon COUNTDOWN20")."""
    for m in CODE_RE.finditer(text):
        code = m.group(1).strip("-")
        if code in NOT_CODES or not re.search(r"[A-Z]", code) or len(code) < 4:
            continue
        if not re.search(r"\d", code) and is_name_word(code, text):
            continue   # a word from the product name ("Glorious ... mouse"), not a code
        if not re.search(r"\d", code) and len(code) < 5:
            continue
        return code
    return None


# --------------------------------------------------------------------------- parallel fetching

WORKERS = CONFIG.get("parallel_requests", 4)   # requests in flight at once; gentle enough for Slickdeals


def search_all(queries):
    """Run Slickdeals searches WORKERS at a time. Returns (items, ok_count). Each item remembers its query.
    A failed search is retried once after a pause, in case the site asked us to slow down."""
    def one(q):
        for attempt in range(2):
            try:
                items = fetch_slickdeals(q)
                for it in items:
                    it["query"] = q
                time.sleep(0.4)
                return items, True
            except Exception as e:
                if attempt:
                    print("slickdeals %r failed: %s" % (q, e))
                    return [], False
                time.sleep(8)
    out, ok = [], 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for items, good in pool.map(one, queries):
            out += items
            ok += good
    return out, ok


def read_thread(link):
    """One Slickdeals deal page -> regular/final price, key-card mention, expired flag."""
    page = get(link, timeout=20).decode("utf-8", "replace")
    lp = re.search(r'"listPrice":"?([\d.]+)', page)
    fp = re.search(r'"finalPrice":"?([\d.]+)', page)
    ex = re.search(r'"isExpiredDeal":"(Yes|No)"', page)
    return {"list": float(lp.group(1)) if lp else None, "final": float(fp.group(1)) if fp else None,
            "keycard": bool(KEYCARD_RE.search(page)),
            "expired": ex is not None and ex.group(1) == "Yes", "checked": NOW.isoformat()}


def prefetch_threads(deals, cache, today, need):
    """Read the deal pages that need(d, cached_entry) says are missing or stale, WORKERS at a time,
    and store them in the cache. Returns how many pages were read."""
    todo = {}
    for d in deals:
        m = re.search(r"/f/(\d+)", d["link"])
        if d["source"] == "Slickdeals" and m and need(d, cache.get(m.group(1))):
            todo[m.group(1)] = d["link"]
    def one(item):
        tid, link = item
        try:
            r = read_thread(link)
        except Exception as e:
            print("thread %s failed: %s" % (tid, e))
            r = None
        time.sleep(0.3)
        return tid, r
    with ThreadPoolExecutor(WORKERS) as pool:
        for tid, r in pool.map(one, todo.items()):
            entry = {"list": None, "final": None}
            entry.update(cache.get(tid) or {})
            entry.update(r or {})
            entry["date"] = today
            cache[tid] = entry
    return len(todo)


def apply_list_price(d, c):
    if not c:
        return
    d["keycard"] = c.get("keycard", False)
    if d["price"] is None and c["final"]:
        d["price"] = c["final"]
    if c["list"] and d["price"] is not None and d["price"] * 1.02 < c["list"] < max(d["price"], 1) * 20:
        d["list_price"] = c["list"]
        d["pct_off"] = round((1 - d["price"] / c["list"]) * 100)


CLOTHING_RE = re.compile(
    r"\b(shirts?|t-?shirts?|tees?|polos?|shorts|trunks|pants|jeans|joggers|leggings|hoodies?|sweatshirts?|"
    r"sweaters?|jackets?|coats?|vests?|dress(es)?|skirts?|socks|underwear|boxers?|briefs|bras?|pajamas|"
    r"shoes|sneakers|boots|sandals|slides|slippers|cleats|hats?|caps?|beanies?|gloves|scarf|apparel|"
    r"clothing|fleece|swim(wear|suit)?|nike|adidas|under armour|levi'?s|columbia|puma|reebok|new balance|"
    r"carhartt|north face|champion|hanes|skechers|crocs|vans|converse|asics)\b", re.I)


ELECTRONICS_WORDS = re.compile(
    r"\b(keyboard|mouse|tablet|ipad|kindle|fire (max|hd|tv)|echo|usb|charg(er|ing)|cables?|laptop|chromebook|"
    r"monitor|tv|speaker|camera|ssd|hard drive|router|wi-?fi|power bank|smart (plug|home|bulb)|bluetooth|"
    r"phone|iphone|galaxy|pixel|printer|drone|projector|gpu|cpu|ram|ddr\d|hdmi)\b", re.I)


def deal_group(d):
    if CLOTHING_RE.search(d["title"]):
        return "Clothing & Shoes"
    if d["category"] != "Anything" or ELECTRONICS_WORDS.search(d["title"]):
        return "Electronics & Games"
    return "Home & More"


def pick_super_deals(pool, cache, today, excl):
    """Best 3 deals of the day in ANY category: big verified discount + strong community votes."""
    cands = []
    for d in pool:
        if d["source"] != "Slickdeals" or not (d["frontpage"] or d["thumbs"] >= 15):
            continue
        if excl.search(d["title"]) or REFURB_RE.search(d["title"]) or TICKET_RE.search(d["title"]):
            continue
        if re.search(r"\b(e-?books?|kindle edition|audiobooks?|audible|digital|app store|google play|"
                     r"xbox(\.| )com|xbox game sale|xbox store|playstation store|ps store|eshop|nintendo\.com|"
                     r"pc games?|steam|gog|epic games|fanatical|humble|green man gaming|game code|dlc)\b",
                     d["title"] + " " + (d.get("store_hint") or ""), re.I):
            continue
        if AUDIO_RE.search(d["title"]) and not APPLE_RE.search(d["title"]):
            continue
        age = 99
        if d["posted"]:
            age = (NOW - datetime.fromisoformat(d["posted"])).total_seconds() / 86400
        if age > 3:
            continue
        price, lst, pct = parse_prices(d)
        if detect_store(d) == "eBay" and not ebay_seller(d):
            continue
        if detect_category(d["title"]) == "Phone & Data Plans":
            continue
        cands.append(dict(d, price=price, list_price=lst, pct_off=pct, age_days=age,
                          store=detect_store(d), category=detect_category(d["title"]) or "Anything",
                          key=product_key(d["title"])))
    cands.sort(key=lambda x: -x["thumbs"])
    cands = cands[:70]
    fetched = 0
    for d in cands:
        c, f = thread_info(d, cache, today)
        fetched += f
        apply_list_price(d, c)
    for d in cands:
        text = d["title"] + " " + d["desc"] + " " + d.get("body", "")[:600]
        d["code"] = find_code(text)
        d["clip"] = bool(CLIP_RE.search(text))
        d["name"] = clean_title(d["title"])
    good = [d for d in cands if d["price"] is not None and d.get("pct_off") and 50 <= d["pct_off"] <= 97
            and not is_expired(d, cache, recheck=False)[0]
            and not is_food(d["title"])
            and not (d["store"] in CONFIG.get("apparel_skip_stores", [])
                     and (d["category"] in APPAREL_CATS or CLOTHING_RE.search(d["title"])))]
    for d in good:
        saved = d["list_price"] - d["price"] if d.get("list_price") else 0
        d["group"] = deal_group(d)
        d["super_score"] = round(min(35, 8 * math.log2(1 + max(d["thumbs"], 0)))       # votes
                                 + min(40, max(0, d["pct_off"] - 40) * 0.8)              # % off (80%+ shines)
                                 + min(20, 6 * math.log2(1 + saved / 10))                # dollars saved
                                 + 5 * max(0.0, 1 - d["age_days"] / 3))                  # freshness
    good.sort(key=lambda x: -x["super_score"])

    def similar(a, b):
        x, y = set(a["key"].split()), set(b["key"].split())
        return len(x & y) / max(1, len(x | y)) >= 0.5

    picks = []
    # 80%+ off first, one per group (clothing, electronics, home...), then fill from 50%+
    for tier in ([d for d in good if d["pct_off"] >= 80], good):
        for d in tier:
            if len(picks) == 3:
                break
            if d in picks or any(similar(d, o) for o in picks):
                continue
            if any(o["group"] == d["group"] for o in picks) and len({x["group"] for x in good}) > len(picks):
                continue
            picks.append(d)
    if len(picks) < 3:  # not enough distinct groups: allow repeats
        for d in good:
            if len(picks) == 3:
                break
            if d not in picks and not any(similar(d, o) for o in picks):
                picks.append(d)
    picks.sort(key=lambda x: -x["pct_off"])
    for d in picks:
        d["image"] = embed_image(d["image"])
        for k in ("body", "store_hint", "key"):
            d.pop(k, None)
        d["desc"] = re.sub(r"\s+", " ", d["desc"]).strip()[:220]
    print("Super deals: %d picked from %d candidates (%d new pages read)" % (len(picks), len(cands), fetched))
    return picks


# --------------------------------------------------------------------------- alerts

WATCH_FILE = DATA_DIR / "watchlist.json"     # written by the page through server.py
ALERTED_FILE = DATA_DIR / "alerted.json"     # deals already announced, so nothing alerts twice


def words(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s).split()


def watch_hit(title, price, watch):
    """Same rule as the page: every word of the term is in the title (plural-tolerant), price under the max."""
    t = set(words(title))
    for w in watch:
        ws = words(w.get("term", ""))
        if ws and all(x in t or x + "s" in t or (x.endswith("s") and x[:-1] in t) for x in ws):
            if w.get("max_price") is None or price is None or price <= w["max_price"]:
                return w["term"]
    return None


def notify(title, message):
    """macOS notification (Notification Center)."""
    script = "display notification %s with title %s sound name \"Glass\"" % (
        json.dumps(message[:220]), json.dumps(title))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as e:
        print("notification failed: %s" % e)


def send_alerts(deals, pokecenter):
    if sys.platform != "darwin":
        return   # alerts are Mac notifications; cloud runs (GitHub) skip them so the Mac still gets each one
    try:
        watch = json.loads(WATCH_FILE.read_text())
    except Exception:
        watch = CONFIG.get("default_watchlist", [])
    try:
        alerted = json.loads(ALERTED_FILE.read_text())
    except Exception:
        alerted = {}
    hits = []
    for d in sorted(deals, key=lambda x: -x["score"]):
        term = watch_hit(d["title"], d.get("price"), watch)
        if term and d["id"] not in alerted:
            hits.append(d)
    drops = [d for d in pokecenter if d.get("posted") and d["link"] not in alerted
             and (NOW - datetime.fromisoformat(d["posted"].replace("Z", "+00:00"))).total_seconds() < 86400]
    for d in hits[:3]:
        price = ("$%.2f" % d["price"]).replace(".00", "") if d.get("price") is not None else ""
        notify("Deal Radar: watchlist match", "%s %s at %s" % (d["title"][:150], ("· " + price) if price else "", d["store"]))
    if len(hits) > 3:
        notify("Deal Radar", "%d more watchlist matches. Open Deal Radar to see them." % (len(hits) - 3))
    for d in drops[:3]:
        notify("Deal Radar: Pokémon Center drop", d["title"][:200])
    if len(drops) > 3:
        notify("Deal Radar", "%d more Pokémon Center drops." % (len(drops) - 3))
    for d in hits:
        alerted[d["id"]] = NOW.date().isoformat()
    for d in drops:
        alerted[d["link"]] = NOW.date().isoformat()
    cutoff = NOW.toordinal() - 21
    alerted = {k: v for k, v in alerted.items() if datetime.fromisoformat(v).toordinal() >= cutoff}
    ALERTED_FILE.write_text(json.dumps(alerted))
    print("Alerts: %d watchlist matches, %d Pokémon Center drops" % (len(hits), len(drops)))


QUERY_STATS = DATA_DIR / "query-stats.json"


def log_query_yield(deals):
    """How many deals on the page each search found (a deal found by several searches counts for each).
    Last 28 updates are kept; searches that keep scoring 0 can be removed from config.json."""
    try:
        stats = json.loads(QUERY_STATS.read_text())
    except Exception:
        stats = {}
    counts = {}
    for k, v in CONFIG.items():
        if k.endswith("_queries") and isinstance(v, list):
            for q in v:
                counts.setdefault(q, 0)
    for d in deals:
        for q in d.get("queries", []):
            counts[q] = counts.get(q, 0) + 1
    stats[NOW.strftime("%Y-%m-%d %H:%M")] = counts
    stats = dict(sorted(stats.items())[-28:])
    QUERY_STATS.write_text(json.dumps(stats, indent=1))


def cap_sections(deals):
    """Best-scored deals first, at most max_deals_per_section per section, max_deals_in_page overall."""
    per = CONFIG.get("max_deals_per_section", 110)
    count, out = {}, []
    for d in sorted(deals, key=lambda x: -x["score"]):
        sec = SECTION_OF.get(d["category"])
        if sec:
            if count.get(sec, 0) >= per:
                continue
            count[sec] = count.get(sec, 0) + 1
        out.append(d)
    return out[: CONFIG["max_deals_in_page"]]


def main():
    DATA_DIR.mkdir(exist_ok=True)
    raw, sources = [], []

    # first, before the other Reddit requests use up the rate limit
    pokecenter = []
    try:
        pokecenter = fetch_pokecenter()
        sources.append({"name": "Pokémon Center drops", "ok": True})
    except Exception as e:
        sources.append({"name": "Pokémon Center drops", "ok": False, "error": str(e)})

    # Reddit runs in the background while the Slickdeals searches go (it has its own rate-limit pauses)
    def reddit_all():
        items, srcs = [], []
        for sub, default_cat in CONFIG.get("reddit_subs", {}).items():
            try:
                got = fetch_reddit(sub)
                for it in got:
                    it["default_category"] = default_cat
                items += got
                srcs.append({"name": "r/" + sub, "ok": True})
            except Exception as e:
                srcs.append({"name": "r/" + sub, "ok": False, "error": str(e)})
            time.sleep(6)
        return items, srcs
    bg = ThreadPoolExecutor(1)
    reddit_job = bg.submit(reddit_all)

    try:
        raw += fetch_slickdeals(frontpage=True)
        sources.append({"name": "Slickdeals Frontpage", "ok": True})
    except Exception as e:
        sources.append({"name": "Slickdeals Frontpage", "ok": False, "error": str(e)})

    t0 = time.time()
    got, sd_ok = search_all(CONFIG["slickdeals_queries"])
    raw += got
    sources.append({"name": "Slickdeals searches (%d/%d)" % (sd_ok, len(CONFIG["slickdeals_queries"])),
                    "ok": sd_ok > 0})
    extra = [q for q in CONFIG.get("apparel_queries", []) + CONFIG.get("toy_queries", [])
             + CONFIG.get("beauty_queries", []) + CONFIG.get("home_queries", []) + CONFIG.get("outdoor_queries", [])
             + CONFIG.get("media_queries", []) + CONFIG.get("art_queries", []) + CONFIG.get("produce_queries", [])
             + CONFIG.get("store_queries", [])
             if q not in CONFIG["slickdeals_queries"] and q not in CONFIG.get("super_deal_queries", [])]
    extra = list(dict.fromkeys(extra))
    got, ex_ok = search_all(extra + CONFIG.get("super_deal_queries", []))
    raw += got
    if extra:
        sources.append({"name": "Store, apparel, media, art, outdoor, self care & home searches (%d/%d)"
                        % (min(ex_ok, len(extra)), len(extra)), "ok": ex_ok > 0})
    print("Searches: %d in %.0fs" % (len(CONFIG["slickdeals_queries"]) + len(extra)
                                     + len(CONFIG.get("super_deal_queries", [])), time.time() - t0))

    for name, url in CONFIG.get("rss_feeds", {}).items():
        try:
            got = fetch_rss(name, url)
            raw += got
            sources.append({"name": name, "ok": True})
        except Exception as e:
            sources.append({"name": name, "ok": False, "error": str(e)})

    items, srcs = reddit_job.result()
    bg.shutdown()
    raw += items
    sources += srcs

    # dedupe (frontpage copy wins); remember every search that found the deal
    found_by = {}
    for d in raw:
        if d.get("query"):
            found_by.setdefault(d["id"], set()).add(d["query"])
    seen = {}
    for d in raw:
        if d["id"] not in seen or d["frontpage"]:
            seen[d["id"]] = d

    excl = re.compile(r"\b(" + "|".join(re.escape(w) for w in CONFIG["exclude_words"]) + r")\b", re.I)
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
    today = NOW.date().isoformat()

    deals = []
    for d in seen.values():
        refurb = bool(REFURB_RE.search(d["title"]))
        hit = excl.search(REFURB_RE.sub(" ", d["title"]) if refurb else d["title"])
        if hit or TICKET_RE.search(d["title"]):
            continue
        cat = detect_category(d["title"]) or d.get("default_category")
        if cat == "Phone & Data Plans":
            continue
        if not cat and detect_store(d) in CONFIG.get("self_care_stores", []):
            cat = "Personal Care"
        if not cat and detect_store(d) in CONFIG.get("home_stores", []):
            cat = "Home & Goods"
        if not cat and d["frontpage"] and d["thumbs"] >= CONFIG.get("home_min_votes", 15):
            cat = "Home & Goods"
        if not cat:
            continue
        if refurb and cat not in CONFIG.get("refurb_ok_categories", []):
            continue
        if AUDIO_RE.search(d["title"]) and not APPLE_RE.search(d["title"]):
            continue
        # games linked from a console maker's online store are eShop / digital downloads
        if cat == "Games" and re.search(DIGITAL_STORES, (d.get("store_hint") or "") + " " + d["body"], re.I):
            continue
        d["refurbished"] = refurb
        d["queries"] = sorted(found_by.get(d["id"], ()))
        if (cat == "Controllers & Accessories" and CONTROLLER_RE.search(d["title"])
                and not is_first_party_controller(d["title"])):
            continue
        age = None
        if d["posted"]:
            try:
                p = datetime.fromisoformat(d["posted"].replace("Z", "+00:00"))
                age = (NOW - p).total_seconds() / 86400
            except ValueError:
                pass
        if age is not None and age > CONFIG["max_age_days"]:
            continue
        price, lst, pct = parse_prices(d)
        if d["source"].startswith("r/"):
            if re.search(r"mega ?thread|discord|informational|\bq ?(&|/) ?a\b|rules|"
                         r"^\[(meta|request|discussion|question|psa)\]", d["title"], re.I):
                continue
            if price is None and d.get("default_category") == "Pokémon TCG":
                cat = "Drops & Restocks"
        d.update(category=cat, store=detect_store(d), price=price, list_price=lst,
                 pct_off=pct, age_days=age, key=product_key(d["title"]))
        if cat in APPAREL_CATS and d["store"] in CONFIG.get("apparel_skip_stores", []):
            continue
        if d["store"] == "eBay":
            seller = ebay_seller(d)
            if not seller:
                continue   # third-party or unknown eBay seller
            d["official_seller"] = seller
            d["marketplace"] = False
        if cat in HOME_CATS and is_food(d["title"]):
            continue
        d["msrp"] = msrp_check(d)

        # price history (one point per product per store per day)
        if price is not None:
            h = history.setdefault(d["key"], [])
            prior = [x["price"] for x in h if x["date"] != today]
            d["lowest_seen"] = bool(prior) and price < min(prior)
            same_store = [x["price"] for x in h if x["date"] != today and x["store"] == d["store"]]
            d["prev_price"] = max(same_store) if same_store and max(same_store) > price else None
            d["history_points"] = len(prior)
            if not any(x["date"] == today and x["store"] == d["store"] for x in h):
                h.append({"date": today, "price": price, "store": d["store"]})
        d["score"] = score(d)
        deals.append(d)

    # regular ("list") price from each Slickdeals thread page, for the deals that make the page
    deals.sort(key=lambda x: -x["score"])
    cache = json.loads(THREAD_CACHE.read_text()) if THREAD_CACHE.exists() else {}
    t0 = time.time()
    fetched = prefetch_threads(deals[: CONFIG["max_deals_in_page"]], cache, today,
                               lambda d, c: c is None or (is_switch2_game(d) and "keycard" not in c))
    for d in deals[: CONFIG["max_deals_in_page"]]:
        c, _ = thread_info(d, cache, today)
        apply_list_price(d, c)
    print("Read %d new deal pages for regular prices in %.0fs" % (fetched, time.time() - t0))
    super_deals = pick_super_deals(list(seen.values()), cache, today, excl)
    keep_after = NOW.toordinal() - 30
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v["date"]).toordinal() >= keep_after}
    THREAD_CACHE.write_text(json.dumps(cache))

    # Switch 2: hide Game-Key Card releases and digital upgrade packs
    kc_titles = [norm_title(t) for t in CONFIG.get("switch2_keycard_titles", [])]
    def keycard(d):
        if not is_switch2_game(d):
            return False
        text = d["title"] + " " + d["desc"]
        return (d.get("keycard") or bool(KEYCARD_RE.search(text))
                or re.search(r"\bupgrade\b", d["title"], re.I) is not None
                or any(k and k in norm_title(d["title"]) for k in kc_titles))
    hidden = [d["title"] for d in deals if keycard(d)]
    deals = [d for d in deals if not keycard(d)]
    if hidden:
        print("Hid %d Switch 2 key-card/upgrade listings" % len(hidden))

    for d in deals:
        # fall back to a higher price recorded earlier at the same store
        if not d.get("list_price") and d.get("prev_price") and d["price"] is not None:
            d["list_price"] = d["prev_price"]
            d["pct_off"] = round((1 - d["price"] / d["prev_price"]) * 100)
            d["was_from_history"] = True
        if d.get("pct_off") is not None and not (3 <= d["pct_off"] <= 95):
            d["pct_off"], d["list_price"] = None, None
        d["score"] = score(d)

    # group the same product across stores
    deals.sort(key=lambda x: -x["score"])
    for d in deals:
        a = set(d["key"].split())
        others = []
        for o in deals:
            if o is d or o["category"] != d["category"] or o["price"] is None:
                continue
            b = set(o["key"].split())
            if a and b and len(a & b) / len(a | b) >= 0.6 and o["store"] != d["store"]:
                others.append({"store": o["store"], "price": o["price"], "link": o["link"]})
        d["compare"] = sorted(others, key=lambda x: x["price"])[:4]
        d["cheapest"] = d["price"] is not None and all(d["price"] <= o["price"] for o in d["compare"])

    deals = cap_sections(deals)

    # drop expired deals: re-check the deals likely to be top picks (posted 6+ hours ago, page last read
    # 8+ hours ago), several at a time; trust the cached status for the rest
    t0 = time.time()
    rank, recheck = {}, []
    for d in deals:
        sec = SECTION_OF.get(d["category"], d["category"])
        rank[sec] = rank.get(sec, 0) + 1
        if rank[sec] <= CONFIG.get("top_picks_per_section", 24) + 6 and (d["age_days"] or 0) >= 0.25:
            recheck.append(d)
    def stale(d, c):
        return not c or not c.get("checked") or \
            (NOW - datetime.fromisoformat(c["checked"])).total_seconds() >= EXPIRY_RECHECK_HOURS * 3600
    checked = prefetch_threads(recheck, cache, today, stale)
    dead, kept = 0, []
    for d in deals:
        expired, _ = is_expired(d, cache, recheck=False)
        if expired:
            dead += 1
            continue
        kept.append(d)
    deals = kept
    THREAD_CACHE.write_text(json.dumps(cache))
    print("Expired: removed %d (checked %d deal pages in %.0fs)" % (dead, checked, time.time() - t0))

    for d in deals:
        text = d["title"] + " " + d["desc"] + " " + d.get("body", "")[:600]
        d["code"] = find_code(text)
        d["clip"] = bool(CLIP_RE.search(text))
        d["name"] = clean_title(d["title"])
    log_query_yield(deals)
    # 30-day price trail per product (lowest price seen each day, any store), for the card's mini chart
    since = (NOW.toordinal() - 30)
    for d in deals:
        pts = {}
        for x in history.get(d.get("key"), []):
            if datetime.fromisoformat(x["date"]).toordinal() >= since:
                pts[x["date"]] = min(pts.get(x["date"], 1e9), x["price"])
        if len(pts) >= 3:
            d["hist"] = [[k, round(v, 2)] for k, v in sorted(pts.items())]
    for d in deals:
        d["image"] = embed_image(d["image"])
        d["top_pick"] = False
        d.pop("queries", None)
        for k in ("body", "store_hint", "key", "query"):
            d.pop(k, None)
        d["desc"] = re.sub(r"\s+", " ", d["desc"]).strip()[:220]

    # top picks: the best few in each section, above a minimum score (deals are already sorted by score)
    rank = {}
    for d in sorted(deals, key=lambda x: -x["score"]):
        sec = SECTION_OF.get(d["category"], d["category"])
        rank[sec] = rank.get(sec, 0) + 1
        d["top_pick"] = (d["score"] >= CONFIG["top_pick_min_score"]
                         and rank[sec] <= CONFIG.get("top_picks_per_section", 24))

    # prune history older than 120 days
    cutoff = NOW.toordinal() - 120
    for k in list(history):
        history[k] = [x for x in history[k]
                      if datetime.fromisoformat(x["date"]).toordinal() >= cutoff]
        if not history[k]:
            del history[k]
    HISTORY_FILE.write_text(json.dumps(history))

    payload = {
        "updated": NOW.isoformat(),
        "sources": sources,
        "default_watchlist": CONFIG["default_watchlist"],
        "deals": deals,
        "super_deals": super_deals,
        "pokecenter": pokecenter,
        # store icons, embedded once and shown next to each store name
        "logos": store_logos.logos_for([d["store"] for d in deals] + [d["store"] for d in super_deals]),
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False))

    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.read_text().replace("/*__DEALS_JSON__*/null", blob)
    OUT_HTML.write_text(page)

    send_alerts(deals, pokecenter)

    tops = sum(1 for d in deals if d["top_pick"])
    print("Built %s: %d deals (%d top picks), %.1f MB" % (
        OUT_HTML.name, len(deals), tops, OUT_HTML.stat().st_size / 1e6))
    for s in sources:
        print("  %s %s" % ("ok " if s["ok"] else "ERR", s["name"]))


if __name__ == "__main__":
    # one refresh at a time (scheduled run and the page's "Update now" button share this lock)
    DATA_DIR.mkdir(exist_ok=True)
    lock = open(DATA_DIR / ".refresh.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Another update is already running.")
    main()
