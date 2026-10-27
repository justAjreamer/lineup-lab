"""One-off: save ESPN odds samples (today's scoreboard, yesterday's scoreboard, one final summary's odds blocks)."""
import json, os, requests
os.makedirs("samples", exist_ok=True)
B = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"; H = {"User-Agent": "Mozilla/5.0"}
for d in ["20261027", "20261026"]:
    sb = requests.get(f"{B}/scoreboard?dates={d}", headers=H, timeout=30).json()
    json.dump([{"id": e["id"], "status": e["status"]["type"]["name"], "odds": e["competitions"][0].get("odds")} for e in sb["events"]],
              open(f"samples/odds_scoreboard_{d}.json", "w"), indent=1)
s = requests.get(f"{B}/summary?event=401909870", headers=H, timeout=30).json()
json.dump({k: s.get(k) for k in ("pickcenter", "odds", "againstTheSpread")}, open("samples/odds_summary_401909870.json", "w"), indent=1)
print("ok")
