#!/usr/bin/env python3
"""Exit 0 once TODAY's NSE end-of-day archives are published:
equity bhavcopy (delivery %) and F&O bhavcopy (OI/PCR/buildup).
nightly.sh polls this so the analysis never runs on yesterday's data
just because it started early. Exit 1 = not up yet (or a holiday).
"""
import sys
from datetime import date

import requests

H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36")}
d = date.today()
URLS = (
    "https://archives.nseindia.com/products/content/"
    f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv",
    "https://archives.nseindia.com/content/fo/"
    f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip",
)
for url in URLS:
    try:
        r = requests.get(url, headers=H, timeout=20, stream=True)
        first = next(r.iter_content(1), b"")
        r.close()
        if r.status_code != 200 or first.startswith(b"<"):
            print(f"not yet: {url.rsplit('/', 1)[1]}")
            sys.exit(1)
    except requests.RequestException as e:
        print(f"probe error: {e}")
        sys.exit(1)
print("EOD files present")
