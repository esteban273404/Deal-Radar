"""Store logos: one small icon per store, downloaded once into data/logos/ and embedded in the page.
Uses each store's site icon (favicon) via Google's favicon service."""
import base64, json, os, re, subprocess
from pathlib import Path

DIR = Path(__file__).resolve().parent / os.environ.get("DEAL_RADAR_DATA", "data") / "logos"
DOMAINS = {
    "Amazon": "amazon.com", "Walmart": "walmart.com", "Target": "target.com", "Best Buy": "bestbuy.com",
    "Costco": "costco.com", "Sam's Club": "samsclub.com", "eBay": "ebay.com", "Woot": "woot.com",
    "Newegg": "newegg.com", "GameStop": "gamestop.com", "Nintendo": "nintendo.com", "LEGO": "lego.com",
    "Pokémon Center": "pokemoncenter.com", "Macy's": "macys.com", "Kohl's": "kohls.com",
    "Dick's": "dickssportinggoods.com", "Academy": "academy.com", "Zappos": "zappos.com", "REI": "rei.com",
    "Nike": "nike.com", "Adidas": "adidas.com", "New Balance": "newbalance.com", "Nordstrom": "nordstrom.com",
    "Nordstrom Rack": "nordstromrack.com", "Old Navy": "oldnavy.gap.com", "Gap": "gap.com",
    "Banana Republic": "bananarepublic.gap.com", "J.Crew": "jcrew.com", "Patagonia": "patagonia.com",
    "Levi's": "levi.com", "American Eagle": "ae.com", "Hollister": "hollisterco.com",
    "Abercrombie": "abercrombie.com", "Uniqlo": "uniqlo.com", "Zara": "zara.com", "Lululemon": "lululemon.com",
    "The North Face": "thenorthface.com", "Columbia": "columbia.com", "Carhartt": "carhartt.com",
    "Under Armour": "underarmour.com", "Ulta": "ulta.com", "Sephora": "sephora.com",
    "Bath & Body Works": "bathandbodyworks.com", "Walgreens": "walgreens.com", "CVS": "cvs.com",
    "IKEA": "ikea.com", "The Container Store": "containerstore.com", "Samsung": "samsung.com",
    "Dell": "dell.com", "Lenovo": "lenovo.com", "HP": "hp.com", "Apple": "apple.com",
    "Micro Center": "microcenter.com", "B&H": "bhphotovideo.com", "Staples": "staples.com",
    "Bass Pro": "basspro.com", "Cabela's": "cabelas.com", "Backcountry": "backcountry.com",
    "Michaels": "michaels.com", "Blick": "dickblick.com", "JetPens": "jetpens.com", "Muji": "muji.us",
    "Barnes & Noble": "barnesandnoble.com", "Albertsons": "albertsons.com", "Safeway": "safeway.com",
    "Whole Foods": "wholefoodsmarket.com", "Kroger": "kroger.com", "Lowe's": "lowes.com",
    "Home Depot": "homedepot.com", "Wayfair": "wayfair.com", "Jomashop": "jomashop.com",
}


def logo(store):
    """data: URI for a store's icon, or None. Cached on disk so each icon is downloaded once."""
    dom = DOMAINS.get(store)
    if not dom:
        return None
    f = DIR / (re.sub(r"[^a-z0-9]+", "-", dom) + ".png")
    if not f.exists():
        DIR.mkdir(exist_ok=True)
        r = subprocess.run(["curl", "-sSL", "--fail", "--max-time", "15",
                            "https://www.google.com/s2/favicons?sz=64&domain=" + dom], capture_output=True)
        if r.returncode != 0 or len(r.stdout) < 100:
            return None
        f.write_bytes(r.stdout)
    return "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()


def logos_for(stores):
    out = {}
    for s in sorted(set(stores)):
        u = logo(s)
        if u:
            out[s] = u
    return out
