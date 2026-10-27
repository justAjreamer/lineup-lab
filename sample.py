"""Save raw samples of each working source to samples/ (fixtures for offline development)."""
import json, os, re, requests
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Referer": "https://www.nba.com/"}
os.makedirs("samples", exist_ok=True)
B = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
def save(name, url, kind="json"):
    try:
        r = requests.get(url, headers=UA, timeout=30); print(name, r.status_code, len(r.content))
        open(f"samples/{name}", "wb").write(r.content); return r
    except Exception as e:
        print(name, "ERR", e)
sb = save("espn_scoreboard_20261026.json", f"{B}/scoreboard?dates=20261026")
save("espn_scoreboard_range.json", f"{B}/scoreboard?dates=20261020-20261031&limit=1000")
if sb is not None and sb.ok:
    ev = sb.json()["events"]
    for e in ev[:2]:
        save(f"espn_summary_{e['id']}.json", f"{B}/summary?event={e['id']}")
save("espn_injuries.json", f"{B}/injuries")
idx = save("nba_injury_index.html", "https://official.nba.com/nba-injury-report-2026-27-season/")
if idx is not None and idx.ok:
    links = sorted(set(re.findall(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[\w\-]+\.pdf", idx.text)))
    print("pdf links:", len(links), links[-3:])
    if links:
        save("nba_injury_latest.pdf", links[-1])
