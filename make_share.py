"""Build the shareable copy of deals.html (data/deals-share.html).

Same page, minus the "Update now" button, which only works on this Mac.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "deals.html"
DATA = ROOT / os.environ.get("DEAL_RADAR_DATA", "data")
OUT = DATA / "deals-share.html"
HIDE = "<style>.refresh { display: none !important; }</style>\n"

html = SRC.read_text()
OUT.write_text(html.replace("</style>", "</style>\n" + HIDE, 1))
print(OUT)
