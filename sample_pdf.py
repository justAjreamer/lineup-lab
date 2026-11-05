"""One-off: save the latest official injury report PDF for parser debugging."""
import os, re, requests
os.makedirs("samples", exist_ok=True)
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/"}
html = requests.get("https://official.nba.com/nba-injury-report-2026-27-season/", headers=H, timeout=30).text
links = sorted(set(re.findall(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[\w\-]+\.pdf", html)))
print(len(links), links[-3:])
open("samples/index_links.txt", "w").write("\n".join(links))
for u in links[-2:]:
    open("samples/" + u.rsplit("/", 1)[1], "wb").write(requests.get(u, headers=H, timeout=30).content)
