"""One-off: save a raw ESPN game summary (with play-by-play) to samples/ for offline parser development."""
import json, os, requests
os.makedirs("samples", exist_ok=True)
B = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
H = {"User-Agent": "Mozilla/5.0"}
for eid in ["401909869", "401909868", "401909870"]:   # MIN@CLE, WSH@CHA, CHI@BOS on Oct 26
    s = requests.get(f"{B}/summary?event={eid}", headers=H, timeout=30).json()
    json.dump({"plays": s.get("plays", []), "boxscore": s.get("boxscore"), "header": s.get("header")}, open(f"samples/pbp_{eid}.json", "w"))
    print(eid, len(s.get("plays", [])))
