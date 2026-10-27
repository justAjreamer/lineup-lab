"""Official NBA data feed: box scores, schedule/results and availability, kept as a game-by-game ledger.

Commands (run from the project root, or standalone in the GitHub relay):
  python3 -m lineuplab.nba_feed fetch  [--root DIR]   pull new data from NBA.com into DIR/ (needs network access)
  python3 -m lineuplab.nba_feed build                 turn the ledger into the season tables the model reads
  python3 -m lineuplab.nba_feed update                fetch directly; if blocked, pull the GitHub relay; then build

Why a ledger: every completed game is stored once (data/2026-27/ledger/<gameId>.json) and never refetched, and the
season tables are re-derived from it, so a missed day heals itself the next time any route works.

Sources (all public, no key):
  schedule  https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json
  box score https://cdn.nba.com/static/json/liveData/boxscore/boxscore_<gameId>.json  (minutes, starters, DNP reasons)
  injuries  official NBA injury report PDFs linked from https://official.nba.com/nba-injury-report-<season>-season/
This file depends only on the standard library + requests (+ pypdf for the injury report) so the relay can run it alone.
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOX_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{}.json"
INJURY_INDEX = "https://official.nba.com/nba-injury-report-{}-season/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com", "Accept": "application/json, text/plain, */*"}
SEASON = "2026-27"
try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # no tz database: fixed CDT offset (an hour off in winter; dates are still right)
    CT = dt.timezone(dt.timedelta(hours=-5))


def _root(args):
    if "--root" in args:
        return args[args.index("--root") + 1]
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", SEASON)


# ------------------------------------------------------------------ fetch
def _get(url, kind="json"):
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json() if kind == "json" else (r.content if kind == "bytes" else r.text)


def _mins(iso):
    """'PT34M12.00S' -> 34.2"""
    m = re.match(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", iso or "")
    return round(int(m.group(1) or 0) + float(m.group(2) or 0) / 60, 2) if m else 0.0


def compact_box(box):
    """NBA.com live box score -> compact ledger record, validated against the team totals."""
    g = box["game"]
    rec = {"gameId": g["gameId"], "date": (g.get("gameEt") or g.get("gameTimeUTC"))[:10],
           "home": g["homeTeam"]["teamTricode"], "away": g["awayTeam"]["teamTricode"],
           "home_score": g["homeTeam"]["score"], "away_score": g["awayTeam"]["score"],
           "periods": len(g["homeTeam"].get("periods", [])) or 4, "players": []}
    for side in ("homeTeam", "awayTeam"):
        t = g[side]
        for p in t["players"]:
            s = p.get("statistics", {})
            rec["players"].append({
                "team": t["teamTricode"], "name": p.get("name") or f"{p.get('firstName', '')} {p.get('familyName', '')}".strip(),
                "id": p.get("personId"), "starter": p.get("starter") == "1", "played": p.get("played") == "1",
                "status": p.get("status"), "reason": p.get("notPlayingReason"), "desc": p.get("notPlayingDescription"),
                "pos": p.get("position") or "", "min": _mins(s.get("minutes")),
                "fgm": s.get("fieldGoalsMade", 0), "fga": s.get("fieldGoalsAttempted", 0),
                "fg3m": s.get("threePointersMade", 0), "fg3a": s.get("threePointersAttempted", 0),
                "ftm": s.get("freeThrowsMade", 0), "fta": s.get("freeThrowsAttempted", 0),
                "oreb": s.get("reboundsOffensive", 0), "dreb": s.get("reboundsDefensive", 0),
                "ast": s.get("assists", 0), "stl": s.get("steals", 0), "blk": s.get("blocks", 0),
                "tov": s.get("turnovers", 0), "pts": s.get("points", 0), "pm": s.get("plusMinusPoints", 0)})
    rec["problems"] = validate(rec)
    return rec


def validate(rec):
    """A box score is trusted only if players add up to the team: points, and 5 players x game length in minutes."""
    out = []
    for team, score in ((rec["home"], rec["home_score"]), (rec["away"], rec["away_score"])):
        ps = [p for p in rec["players"] if p["team"] == team]
        pts = sum(p["pts"] for p in ps)
        mins = sum(p["min"] for p in ps)
        full = 5 * (48 + 5 * max(0, rec["periods"] - 4))
        if pts != score:
            out.append(f"{team}: players' points {pts} != team score {score}")
        if abs(mins - full) > 2:
            out.append(f"{team}: players' minutes {mins:.1f} != {full}")
        if sum(p["starter"] for p in ps) != 5:
            out.append(f"{team}: {sum(p['starter'] for p in ps)} starters flagged")
    return out


def schedule_games(sched):
    """scheduleLeagueV2 -> [{gameId, date(CT), home, away, status, scores, time_ct}] regular season only."""
    out = []
    for day in sched["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            if not str(g["gameId"]).startswith("002"):
                continue
            utc = dt.datetime.fromisoformat(g["gameDateTimeUTC"].replace("Z", "+00:00"))
            ct = utc.astimezone(CT)
            out.append({"gameId": g["gameId"], "date": ct.strftime("%Y-%m-%d"), "home": g["homeTeam"]["teamTricode"],
                        "away": g["awayTeam"]["teamTricode"], "status": {1: "scheduled", 2: "live", 3: "final"}.get(g["gameStatus"], "scheduled"),
                        "home_score": g["homeTeam"].get("score"), "away_score": g["awayTeam"].get("score"),
                        "time_ct": ct.strftime("%-I:%M %p"), "tip_utc": g["gameDateTimeUTC"]})
    return out


INJ_STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")
TEAM_NAMES = {"Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
              "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
              "Detroit Pistons": "DET", "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
              "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
              "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
              "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
              "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
              "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS"}


def parse_injury_text(text):
    """Text of an official injury report -> [{team, player('First Last'), status, reason}].
    Rows look like '... Boston Celtics Brown, Jaylen Questionable Injury/Illness - Left Knee; Soreness'; the team name
    appears only on its first row, so it carries forward."""
    rows, team = [], None
    names = sorted(TEAM_NAMES, key=len, reverse=True)
    pat = re.compile(r"([A-Z][\w'’.\-]+(?: [A-Z][\w'’.\-]+)*(?: (?:Jr\.|Sr\.|II|III|IV))?), ([A-Z][\w'’.\-]+(?: [A-Z][\w'’.\-]+)*) (" + "|".join(INJ_STATUS) + r")\b(.*)")
    for line in text.splitlines():
        for n in names:
            if n in line:
                team = TEAM_NAMES[n]
                line = line.split(n, 1)[1]
                break
        m = pat.search(line)
        if m and team:
            last, first, status, reason = m.groups()
            suffix = ""
            sm = re.match(r"(.*) (Jr\.|Sr\.|II|III|IV)$", last)
            if sm:
                last, suffix = sm.group(1), " " + sm.group(2)
            rows.append({"team": team, "player": f"{first} {last}{suffix}", "status": status, "reason": reason.strip()})
    return rows


def fetch_injuries(root):
    from io import BytesIO
    from pypdf import PdfReader
    html = _get(INJURY_INDEX.format(SEASON), "text")
    links = re.findall(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[\w\-]+\.pdf", html)
    if not links:
        raise RuntimeError("no injury report links found on the index page")
    url = sorted(set(links))[-1]
    pdf = PdfReader(BytesIO(_get(url, "bytes")))
    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    rows = parse_injury_text(text)
    if not rows:
        raise RuntimeError(f"injury report {url} parsed to 0 rows (format changed?)")
    json.dump({"url": url, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows},
              open(os.path.join(root, "injury_report.json"), "w"), indent=0)
    return len(rows)


def fetch(root):
    """Pull schedule, every new final box score, and the latest injury report. Returns a status dict."""
    os.makedirs(os.path.join(root, "ledger"), exist_ok=True)
    st = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "nba.com direct", "errors": []}
    games = schedule_games(_get(SCHEDULE_URL))
    json.dump({"pulled": dt.datetime.now(CT).strftime("%Y-%m-%d"), "games": games}, open(os.path.join(root, "nba_schedule.json"), "w"))
    have = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(root, "ledger", "*.json"))}
    new = 0
    for g in games:
        if g["status"] != "final" or g["gameId"] in have:
            continue
        try:
            rec = compact_box(_get(BOX_URL.format(g["gameId"])))
            rec["date"] = g["date"]          # use the schedule's CT date so ids line up with the app
            json.dump(rec, open(os.path.join(root, "ledger", g["gameId"] + ".json"), "w"))
            new += 1
        except Exception as e:  # one bad game never blocks the rest; it is retried next run
            st["errors"].append(f"box {g['gameId']}: {e!r}")
    try:
        st["injury_rows"] = fetch_injuries(root)
    except Exception as e:
        st["errors"].append(f"injury report: {e!r}")
    st["new_games"] = new
    st["ledger_games"] = len(have) + new
    json.dump(st, open(os.path.join(root, "feed_status.json"), "w"), indent=1)
    return st


# ------------------------------------------------------------------ build (no network)
def build(root):
    import pandas as pd
    recs = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(root, "ledger", "*.json")))]
    if not recs:
        print("feed build: ledger is empty; nothing to build")
        return None
    bad = {r["gameId"]: r["problems"] for r in recs if r.get("problems")}
    rows = []
    for r in sorted(recs, key=lambda r: (r["date"], r["gameId"])):
        for p in r["players"]:
            rows.append({**p, "gameId": r["gameId"], "date": r["date"], "suspect": r["gameId"] in bad})
    P = pd.DataFrame(rows)
    played = P[P.played & (P["min"] > 0)]
    last = played.sort_values(["date", "gameId"]).groupby("name").tail(1).set_index("name")
    agg = played.groupby("name").agg(G=("gameId", "nunique"), GS=("starter", "sum"), MIN=("min", "sum"),
                                     **{c: (c, "sum") for c in ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov", "pts"]})
    pos = played[played.pos != ""].groupby("name").pos.agg(lambda s: s.mode().iloc[0])
    pg = pd.DataFrame({"Player": agg.index, "Age": "", "Team": last.loc[agg.index, "team"].values,
                       "Pos": [pos.get(n, "") for n in agg.index], "G": agg.G.values, "GS": agg.GS.astype(int).values,
                       "MP": (agg.MIN / agg.G).round(1).values})
    for src, dst in [("fgm", "FG"), ("fga", "FGA"), ("fg3m", "3P"), ("fg3a", "3PA"), ("ftm", "FT"), ("fta", "FTA"), ("oreb", "ORB"),
                     ("dreb", "DRB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK"), ("tov", "TOV"), ("pts", "PTS")]:
        pg[dst] = (agg[src] / agg.G).round(2).values
    pg = pg.sort_values("PTS", ascending=False)
    pg.insert(0, "Rk", range(1, len(pg) + 1))
    pg.to_csv(os.path.join(root, "per_game.csv"), index=False)
    gs = pg[["Player", "Team", "Pos", "G", "GS"]].copy()
    gs["MP"] = (pg.MP * pg.G).round(0)
    gs.to_csv(os.path.join(root, "games_started.csv"), index=False)
    # roster presence: anyone listed active in a box score (includes coach's decisions), latest team
    act = P[P.status == "ACTIVE"].sort_values(["date", "gameId"]).groupby("name").tail(1)
    act.assign(player=act.name, source_game=act.date + " " + act.gameId)[["player", "team", "source_game"]] \
        .to_csv(os.path.join(root, "feed_box_seen.csv"), index=False)
    availability(root, P)
    meta_through = max(r["date"] for r in recs)
    mp = os.path.join(root, "meta.json")
    if os.path.exists(mp):
        m = json.load(open(mp))
        m.update(stats_date=meta_through, season_through=f"2026-27 through {dt.date.fromisoformat(meta_through):%b %-d} (NBA.com box scores)",
                 rosters_as_of=f"NBA.com box scores + injury report, {dt.date.fromisoformat(meta_through):%b %-d}")
        json.dump(m, open(mp, "w"))
    json.dump({"through": meta_through, "games": len(recs), "players": len(pg), "suspect_games": bad},
              open(os.path.join(root, "feed_build.json"), "w"), indent=1)
    print(f"feed build: {len(recs)} games through {meta_through}, {len(pg)} players, {len(bad)} games failing checks")
    return meta_through


def availability(root, P):
    """Auto roster status. Evidence, newest first: today's official injury report; then each player's last two team games.
    Out/Doubtful on the report -> out; Questionable -> questionable; inactive for injury in both of the team's last
    two games -> out; in the last one only -> questionable; played in the team's last game -> active."""
    import pandas as pd
    out = {}
    tg = P.groupby("team").apply(lambda d: sorted(d[["date", "gameId"]].drop_duplicates().itertuples(index=False, name=None))[-2:])
    for team, last2 in tg.items():
        ids = [g for _, g in last2]
        sub = P[(P.team == team) & P.gameId.isin(ids)]
        for name, d in sub.groupby("name"):
            d = d.drop_duplicates("gameId").set_index("gameId").reindex(ids)
            inj = [(isinstance(r, str) and "INJURY" in r) for r in d.reason]
            if bool(d.played.iloc[-1] == True):  # noqa: E712 (reindexed column may hold NaN)
                out[name] = (team, "active", f"played {last2[-1][0]}")
            elif all(inj):
                out[name] = (team, "out", f"inactive last {len(ids)} games: {d.desc.iloc[-1] or 'injury'}")
            elif inj[-1]:
                out[name] = (team, "questionable", f"inactive {last2[-1][0]}: {d.desc.iloc[-1] or 'injury'}")
    rep_path = os.path.join(root, "injury_report.json")
    if os.path.exists(rep_path):
        rep = json.load(open(rep_path))
        for r in rep["rows"]:
            s = {"Out": "out", "Doubtful": "out", "Questionable": "questionable"}.get(r["status"], "active")
            out[r["player"]] = (r["team"], s, f"injury report: {r['status']} — {r['reason']}")
    pd.DataFrame([{"player": k, "team": t, "status": s, "note": n} for k, (t, s, n) in sorted(out.items())]) \
        .to_csv(os.path.join(root, "availability.csv"), index=False)


# ------------------------------------------------------------------ update: direct, else relay
def update(root, relay=None):
    st = None
    try:
        st = fetch(root)
        print(f"feed: direct fetch ok, {st['new_games']} new games, {len(st['errors'])} errors")
    except Exception as e:
        print(f"feed: direct fetch unavailable ({type(e).__name__}); trying relay")
        if relay:
            tmp = tempfile.mkdtemp()
            r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", relay, tmp], capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.isdir(os.path.join(tmp, "data", SEASON, "ledger")):
                src = os.path.join(tmp, "data", SEASON)
                os.makedirs(os.path.join(root, "ledger"), exist_ok=True)
                n = 0
                for f in glob.glob(os.path.join(src, "ledger", "*.json")):
                    dst = os.path.join(root, "ledger", os.path.basename(f))
                    if not os.path.exists(dst):
                        open(dst, "w").write(open(f).read()); n += 1
                for fn in ["nba_schedule.json", "injury_report.json", "feed_status.json"]:
                    if os.path.exists(os.path.join(src, fn)):
                        open(os.path.join(root, fn), "w").write(open(os.path.join(src, fn)).read())
                st = json.load(open(os.path.join(root, "feed_status.json"))) if os.path.exists(os.path.join(root, "feed_status.json")) else {}
                st["source"] = f"GitHub relay ({relay})"
                json.dump(st, open(os.path.join(root, "feed_status.json"), "w"), indent=1)
                print(f"feed: relay ok, {n} new games copied")
            else:
                print(f"feed: relay unavailable ({(r.stderr or 'no ledger in repo').strip()[:120]})")
        else:
            print("feed: no relay configured")
    return build(root), st


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "fetch":
        print(json.dumps(fetch(_root(a)), indent=1))
    elif cmd == "build":
        build(_root(a))
    elif cmd == "update":
        relay = None
        try:
            from lineuplab.config import CFG
            relay = CFG.get("feed", {}).get("relay_repo")
        except Exception:
            pass
        update(_root(a), relay)
    else:
        sys.exit(__doc__)
