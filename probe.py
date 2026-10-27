"""Which NBA data sources answer from GitHub's servers? Writes data/probe.json."""
import json, time, requests
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
VARIANTS = {"browser": {"User-Agent": UA, "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com", "Accept": "application/json, text/plain, */*"},
            "ua_only": {"User-Agent": UA}, "none": {}}
URLS = {
  "nba_cdn_schedule": "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
  "nba_cdn_scoreboard": "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json",
  "nba_cdn_boxscore": "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_0022500001.json",
  "nba_stats": "https://stats.nba.com/stats/scoreboardv2?GameDate=2026-10-26&LeagueID=00&DayOffset=0",
  "nba_injury_index": "https://official.nba.com/nba-injury-report-2026-27-season/",
  "espn_scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261026",
  "espn_injuries": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
  "espn_teams": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams",
  "bref_per_game": "https://www.basketball-reference.com/leagues/NBA_2027_per_game.html",
}
out = {}
for k, u in URLS.items():
    out[k] = {}
    for vn, h in VARIANTS.items():
        try:
            r = requests.get(u, headers=h, timeout=20)
            out[k][vn] = {"status": r.status_code, "bytes": len(r.content), "type": r.headers.get("content-type", "")[:40]}
            if k == "espn_scoreboard" and r.ok and vn == "none":
                ev = r.json().get("events", [])
                out[k]["events"] = [(e["id"], e["shortName"], e["status"]["type"]["name"]) for e in ev]
        except Exception as e:
            out[k][vn] = {"error": repr(e)[:120]}
        time.sleep(1)
# ESPN game summary for one finished game, if the scoreboard listed any: does it carry minutes + starters?
ev = out.get("espn_scoreboard", {}).get("events") or []
if ev:
    try:
        s = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={ev[0][0]}", timeout=20).json()
        team = s["boxscore"]["players"][0]
        stat = team["statistics"][0]
        out["espn_summary_sample"] = {"labels": stat.get("labels"), "keys": stat.get("keys"),
                                      "first_athlete": {k: v for k, v in stat["athletes"][0].items() if k in ("starter", "didNotPlay", "reason", "stats")},
                                      "athlete_name": stat["athletes"][0]["athlete"]["displayName"], "n_athletes": len(stat["athletes"])}
    except Exception as e:
        out["espn_summary_sample"] = {"error": repr(e)[:200]}
json.dump(out, open("data/probe.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:3000])
