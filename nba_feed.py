"""NBA data feed: schedule, results, box scores and availability, kept as a game-by-game ledger.

Commands (run from the project root, or standalone in the GitHub relay):
  python3 -m lineuplab.nba_feed fetch  [--root DIR]   pull new data into DIR/ (needs network access)
  python3 -m lineuplab.nba_feed build                 turn the ledger into the season tables the model reads
  python3 -m lineuplab.nba_feed update                fetch directly; if blocked, pull the GitHub relay; then build

Sources — chosen because they answer from GitHub's servers (NBA.com's own data CDN refuses cloud machines; tested
Oct 27 2026 from the relay):
  ESPN public API   scoreboard (schedule/results), game summary (box score: minutes, starters, positions, DNPs,
                    game-day injuries), league injury list
  NBA official injury report (PDF, linked from official.nba.com): the authoritative game-day status
Every completed game is stored once (data/2026-27/ledger/<id>.json) and never refetched; the season tables are
re-derived from the ledger, so a missed day heals itself the next time any route works.
Standalone needs: python stdlib + requests + pypdf (pandas only for `build`).
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

try:                       # in the GitHub relay, pbp.py sits next to this file
    import pbp as PBP
except ImportError:
    from lineuplab import pbp as PBP

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
INJURY_INDEX = "https://official.nba.com/nba-injury-report-{}-season/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Referer": "https://www.nba.com/", "Accept": "application/json, text/html, */*"}
SEASON, SEASON_YEAR = "2026-27", 2027          # ESPN labels a season by its ending year
ESPN_FIX = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}
try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # no tz database: fixed CDT offset (an hour off in winter; dates are still right)
    CT = dt.timezone(dt.timedelta(hours=-5))
TEAM_NAMES = {"Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
              "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
              "Detroit Pistons": "DET", "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
              "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
              "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
              "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
              "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
              "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS"}
tri = lambda a: ESPN_FIX.get(a, a)


def _root(args):
    if "--root" in args:
        return args[args.index("--root") + 1]
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", SEASON)


def _get(url, kind="json", tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json() if kind == "json" else (r.content if kind == "bytes" else r.text)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


# ------------------------------------------------------------------ parsing (pure functions, tested offline)
def ct_of(iso_utc):
    t = dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(CT)


def scoreboard_games(sb):
    """ESPN scoreboard -> [{id, date (Central), home, away, status, scores, time_ct, tip_utc}], regular season only."""
    out = []
    for e in sb.get("events", []):
        if e.get("season", {}).get("type") != 2:
            continue
        comp = {c["homeAway"]: c for c in e["competitions"][0]["competitors"]}
        if not {tri(c["team"]["abbreviation"]) for c in comp.values()} <= set(TEAM_NAMES.values()):
            continue   # placeholder matchups (e.g. NBA Cup knockout "TBD") until the teams are known
        name = e["status"]["type"]["name"]
        st = "final" if name == "STATUS_FINAL" else "scheduled" if name in ("STATUS_SCHEDULED", "STATUS_POSTPONED") else "live"
        t = ct_of(e["date"])
        g = {"id": f"espn_{e['id']}", "espn_id": e["id"], "date": t.strftime("%Y-%m-%d"), "home": tri(comp["home"]["team"]["abbreviation"]),
             "away": tri(comp["away"]["team"]["abbreviation"]), "status": st, "time_ct": t.strftime("%-I:%M %p"),
             "tip_utc": t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if st == "final":
            g["home_score"], g["away_score"] = int(comp["home"]["score"]), int(comp["away"]["score"])
        if name == "STATUS_POSTPONED":
            g["postponed"] = True
        if st == "scheduled":
            m = market_scoreboard(e["competitions"][0].get("odds"))
            if m and m.get("p_home") is not None:
                g["market"] = m
        out.append(g)
    return out


def _int(x):
    """ESPN stat cell -> int ('--', '' and None mean 0; '+6' -> 6)."""
    x = str(x if x is not None else "").replace("+", "").strip()
    return int(x) if re.fullmatch(r"-?\d+", x) else 0


def _ma(s):
    """'11-22' -> (11, 22); '--' or missing -> (0, 0)."""
    m = re.fullmatch(r"(\d+)-(\d+)", str(s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def compact_summary(s, game):
    """ESPN game summary -> compact ledger record, validated against the final score."""
    rec = {"id": game["id"], "date": game["date"], "home": game["home"], "away": game["away"],
           "home_score": game["home_score"], "away_score": game["away_score"], "source": "espn", "players": []}
    for t in s["boxscore"]["players"]:
        team = tri(t["team"]["abbreviation"])
        st = t["statistics"][0]
        keys = st["keys"]
        for a in st["athletes"]:
            v = dict(zip(keys, a.get("stats") or []))
            played = not a.get("didNotPlay") and bool(a.get("stats"))
            fgm, fga = _ma(v.get("fieldGoalsMade-fieldGoalsAttempted"))
            f3m, f3a = _ma(v.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            ftm, fta = _ma(v.get("freeThrowsMade-freeThrowsAttempted"))
            num = lambda k: _int(v.get(k)) if played else 0
            rec["players"].append({
                "team": team, "name": a["athlete"]["displayName"], "id": a["athlete"].get("id"),
                "starter": bool(a.get("starter")), "played": played, "status": "ACTIVE",
                "reason": None if played else ("DNP_COACH" if "COACH" in str(a.get("reason", "")).upper() else str(a.get("reason") or "DNP")),
                "desc": None, "pos": ((a["athlete"].get("position") or {}).get("abbreviation") or ""),
                "min": float(num("minutes")), "fgm": fgm if played else 0, "fga": fga if played else 0,
                "fg3m": f3m if played else 0, "fg3a": f3a if played else 0, "ftm": ftm if played else 0, "fta": fta if played else 0,
                "oreb": num("offensiveRebounds"), "dreb": num("defensiveRebounds"), "ast": num("assists"), "stl": num("steals"),
                "blk": num("blocks"), "tov": num("turnovers"), "pts": num("points"), "pm": num("plusMinus")})
    listed = {(p["team"], p["name"]) for p in rec["players"]}
    for t in s.get("injuries", []):     # injured players are usually not in the box score at all
        team = tri(t["team"]["abbreviation"])
        for i in t.get("injuries", []):
            nm = i["athlete"]["displayName"]
            if (team, nm) in listed:
                continue
            rec["players"].append({"team": team, "name": nm, "id": None, "starter": False, "played": False, "status": "INACTIVE",
                                   "reason": "INACTIVE_INJURY", "desc": f"{i.get('status', '')}: {(i.get('details') or {}).get('type') or 'injury'}",
                                   "pos": "", "min": 0.0, **{k: 0 for k in ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov", "pts", "pm"]}})
    rec["problems"] = validate(rec)
    return rec


def validate(rec, min_tol=6):
    """Trusted only if the players add up to the team: points exactly; minutes to 5 x game length (ESPN rounds
    minutes to whole numbers, so allow a few); exactly five starters."""
    out = []
    for team, score in ((rec["home"], rec["home_score"]), (rec["away"], rec["away_score"])):
        ps = [p for p in rec["players"] if p["team"] == team]
        pts, mins = sum(p["pts"] for p in ps), sum(p["min"] for p in ps)
        ot = max(0, round((mins - 240) / 25)) if mins > 250 else 0
        if pts != score:
            out.append(f"{team}: players' points {pts} != team score {score}")
        if abs(mins - (240 + 25 * ot)) > min_tol:
            out.append(f"{team}: players' minutes {mins:.0f} != {240 + 25 * ot}")
        if sum(p["starter"] for p in ps) != 5:
            out.append(f"{team}: {sum(p['starter'] for p in ps)} starters flagged")
    return out


def espn_injuries(j):
    """ESPN league injury list -> [{team, player, status, reason, date}]"""
    rows = []
    for t in j.get("injuries", []):
        team = TEAM_NAMES.get(t.get("displayName"))
        for i in t.get("injuries", []):
            d = i.get("details") or {}
            rows.append({"team": team, "player": i["athlete"]["displayName"], "status": i.get("status"),
                         "reason": d.get("type") or "", "date": i.get("date"), "return": d.get("returnDate")})
    return rows


# --- betting market (benchmark only; never a model input)
def _ml(x):
    """'-625' / '+455' / -750 / 525 -> int moneyline, or None"""
    try:
        v = int(str(x).replace("+", "").strip())
        return v if abs(v) >= 100 else None
    except (TypeError, ValueError):
        return None


def novig_home(ml_home, ml_away):
    """Two moneylines -> home win probability with the bookmaker's margin removed (proportional method)."""
    imp = lambda m: 100 / (m + 100) if m > 0 else -m / (-m + 100)
    if ml_home is None or ml_away is None:
        return None
    h, a = imp(ml_home), imp(ml_away)
    return round(h / (h + a), 4)


def market_scoreboard(odds):
    """Scoreboard odds block (upcoming games: the current line) -> {provider, spread, ml_home, ml_away, p_home}"""
    if not odds:
        return None
    o = odds[0]
    mlb = o.get("moneyline") or {}
    mh = _ml(((mlb.get("home") or {}).get("close") or {}).get("odds")) or _ml((o.get("homeTeamOdds") or {}).get("moneyLine"))
    ma = _ml(((mlb.get("away") or {}).get("close") or {}).get("odds")) or _ml((o.get("awayTeamOdds") or {}).get("moneyLine"))
    return {"provider": (o.get("provider") or {}).get("name"), "spread": o.get("spread"), "ml_home": mh, "ml_away": ma,
            "p_home": novig_home(mh, ma)}


def market_pickcenter(pc):
    """Game summary pickcenter (finished games: the closing line) -> same shape"""
    if not pc:
        return None
    o = pc[0]
    mh, ma = _ml((o.get("homeTeamOdds") or {}).get("moneyLine")), _ml((o.get("awayTeamOdds") or {}).get("moneyLine"))
    return {"provider": (o.get("provider") or {}).get("name"), "spread": o.get("spread"), "ml_home": mh, "ml_away": ma,
            "p_home": novig_home(mh, ma), "closing": True}


# --- official NBA injury report: rebuild the table from word positions (text extraction scrambles its rows)
INJ_STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")


def pdf_words(pdf_bytes):
    """-> [[ (x, y, word), ... ] per page]"""
    from io import BytesIO
    from pypdf import PdfReader
    pages = []
    for page in PdfReader(BytesIO(pdf_bytes)).pages:
        words = []
        page.extract_text(visitor_text=lambda text, cm, tm, fd, fs: words.append(
            (round(tm[4] * cm[0] + cm[4], 1), round(tm[5] * cm[3] + cm[5], 1), text.strip())) if text.strip() else None)
        pages.append(words)
    return pages


def parse_injury_words(pages):
    """Word positions -> [{team, player('First Last'), status, reason, matchup}]. Columns are located from the
    header row; rows are read top to bottom; a team cell applies until the next one; reason text that wraps onto
    its own line joins the nearest player row."""
    rows, team, matchup, cols = [], None, None, None
    names = sorted(TEAM_NAMES, key=len, reverse=True)
    for words in pages:
        hdr = {w: x for x, y, w in words if w in ("Matchup", "Team", "Player", "Current", "Reason")}
        if {"Team", "Player", "Current", "Reason"} <= set(hdr):
            hy = max(y for x, y, w in words if w == "Player")
            cols = [("matchup", hdr.get("Matchup", hdr["Team"] - 60)), ("team", hdr["Team"]), ("player", hdr["Player"]),
                    ("status", hdr["Current"]), ("reason", hdr["Reason"])]
        elif cols:   # continuation pages repeat only the title: keep page 1's columns, skip the title line
            title = [y for x, y, w in words if w == "Report:"]
            hy = (max(title) if title else max(y for x, y, w in words) + 1) - 3
        else:
            continue
        lines = {}
        for x, y, w in words:
            if y >= hy - 2:
                continue  # header and title
            key = next((k for k in lines if abs(k - y) <= 2.5), y)
            lines.setdefault(key, []).append((x, w))
        entries = []
        for y in sorted(lines, reverse=True):
            cell = {c: [] for c, _ in cols}
            for x, w in sorted(lines[y]):
                c = [c for c, cx in cols if x >= cx - 6]
                if c:
                    cell[c[-1]].append(w)
            txt = {c: " ".join(v) for c, v in cell.items()}
            if re.search(r"[A-Z]{2,3}@[A-Z]{2,3}", txt["matchup"]):
                matchup = re.search(r"[A-Z]{2,3}@[A-Z]{2,3}", txt["matchup"]).group(0)
            if txt["team"]:
                tn = re.sub(r"\s+", " ", txt["team"])
                hit = next((n for n in names if n in tn or tn in n), None)
                if hit:
                    team = TEAM_NAMES[hit]
            m = re.match(r"(.+?), (.+)$", txt["player"])
            if m and txt["status"].split()[:1] and txt["status"].split()[0] in INJ_STATUS:
                last, first = m.groups()
                sm = re.match(r"(.*) (Jr\.|Sr\.|II|III|IV)$", last)
                name = f"{first} {sm.group(1)} {sm.group(2)}" if sm else f"{first} {last}"
                entries.append({"y": y, "team": team, "player": name, "status": txt["status"].split()[0],
                                "reason": txt["reason"], "matchup": matchup})
            elif txt["reason"] and not txt["player"] and entries is not None:
                entries.append({"y": y, "continuation": txt["reason"]})
        players = [e for e in entries if "player" in e]
        for e in entries:
            if "continuation" in e and players:
                near = min(players, key=lambda p: abs(p["y"] - e["y"]))
                if abs(near["y"] - e["y"]) < 20:
                    near["reason"] = (near["reason"] + " " + e["continuation"]).strip()
        rows += [{k: v for k, v in p.items() if k != "y"} for p in players]
    return rows


# ------------------------------------------------------------------ fetch (network)
def fetch(root):
    """Schedule + results for the season, every new final box score, ESPN and official injury reports."""
    os.makedirs(os.path.join(root, "ledger"), exist_ok=True)
    st = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "ESPN + NBA injury report", "errors": []}
    sched_path = os.path.join(root, "nba_schedule.json")
    cache = json.load(open(sched_path)) if os.path.exists(sched_path) else {"games": [], "full_at": None}
    today = dt.datetime.now(CT).date()
    first = _get(f"{ESPN}/scoreboard?dates={today:%Y%m%d}")
    cal = sorted({c[:10] for c in (first.get("leagues") or [{}])[0].get("calendar", [])})
    stale = not cache.get("full_at") or (dt.date.today() - dt.date.fromisoformat(cache["full_at"])).days >= 7
    known = {g["id"]: g for g in cache["games"]}
    if stale:   # weekly: re-read the whole season (catches reschedules)
        dates = cal
    else:       # otherwise: from the oldest unfinished game in the past through the next 10 days
        open_past = [g["date"] for g in known.values() if g["status"] != "final" and g["date"] <= today.isoformat()]
        start = min(open_past + [(today - dt.timedelta(days=2)).isoformat()])
        dates = [d for d in cal if start <= d <= (today + dt.timedelta(days=10)).isoformat()]
    for d in dates:
        try:
            for g in scoreboard_games(_get(f"{ESPN}/scoreboard?dates={d.replace('-', '')}")):
                known[g["id"]] = g
        except Exception as e:
            st["errors"].append(f"scoreboard {d}: {e!r}")
        time.sleep(0.25)
    cache = {"pulled": today.isoformat(), "full_at": today.isoformat() if stale else cache.get("full_at"),
             "games": sorted(known.values(), key=lambda g: (g["date"], g["home"]))}
    json.dump(cache, open(sched_path, "w"))
    os.makedirs(os.path.join(root, "pbp"), exist_ok=True)
    have = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(root, "ledger", "*.json"))}
    have_pbp = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(root, "pbp", "*.json"))}
    new = new_pbp = 0
    def no_market(gid):   # ledger records from before the market benchmark existed get it backfilled once
        f = os.path.join(root, "ledger", gid + ".json")
        return gid in have and "market" not in json.load(open(f))
    for g in cache["games"]:
        if g["status"] != "final" or (g["id"] in have and g["id"] in have_pbp and not no_market(g["id"])):
            continue
        try:
            summ = _get(f"{ESPN}/summary?event={g['espn_id']}")
            if g["id"] not in have:
                rec = compact_summary(summ, g)
                rec["market"] = market_pickcenter(summ.get("pickcenter"))
                json.dump(rec, open(os.path.join(root, "ledger", g["id"] + ".json"), "w"))
                new += 1
            elif no_market(g["id"]):
                f = os.path.join(root, "ledger", g["id"] + ".json")
                rec = json.load(open(f))
                rec["market"] = market_pickcenter(summ.get("pickcenter"))
                json.dump(rec, open(f, "w"))
            if g["id"] not in have_pbp and summ.get("plays"):
                json.dump(PBP.compact_plays(summ), open(os.path.join(root, "pbp", g["id"] + ".json"), "w"), separators=(",", ":"))
                new_pbp += 1
        except Exception as e:  # one bad game never blocks the rest; it is retried next run
            st["errors"].append(f"game {g['id']}: {e!r}")
        time.sleep(0.25)
    st["new_pbp"] = new_pbp
    try:
        rows = espn_injuries(_get(f"{ESPN}/injuries"))
        json.dump({"fetched_at": st["fetched_at"], "rows": rows}, open(os.path.join(root, "espn_injuries.json"), "w"))
        os.makedirs(os.path.join(root, "injury_reports"), exist_ok=True)
        json.dump({"fetched_at": st["fetched_at"], "rows": rows}, open(os.path.join(root, "injury_reports", f"espn_{today:%Y%m%d}.json"), "w"),
                  separators=(",", ":"))
        st["espn_injury_rows"] = len(rows)
    except Exception as e:
        st["errors"].append(f"ESPN injuries: {e!r}")
    try:
        html = _get(INJURY_INDEX.format(SEASON), "text")
        links = sorted(set(re.findall(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[\w\-]+\.pdf", html)),
                       key=lambda u: report_time(u) or dt.datetime.min)
        if not links:
            raise RuntimeError("no report links on the index page")
        rows = parse_injury_words(pdf_words(_get(links[-1], "bytes")))
        if not rows:
            raise RuntimeError(f"{links[-1]} parsed to 0 rows (format changed?)")
        rep = {"url": links[-1], "report_time": str(report_time(links[-1])), "fetched_at": st["fetched_at"], "rows": rows}
        json.dump(rep, open(os.path.join(root, "injury_report.json"), "w"), indent=0)
        # archive: each report kept once, so we can later measure how often each status really plays
        os.makedirs(os.path.join(root, "injury_reports"), exist_ok=True)
        arch = os.path.join(root, "injury_reports", re.sub(r"[^0-9]", "", rep["report_time"])[:12] + ".json")
        if not os.path.exists(arch):
            json.dump(rep, open(arch, "w"), separators=(",", ":"))
        st["official_injury_rows"] = len(rows)
    except Exception as e:
        st["errors"].append(f"official injury report: {e!r}")
    st.update(new_games=new, ledger_games=len(have) + new, schedule_games=len(cache["games"]))
    json.dump(st, open(os.path.join(root, "feed_status.json"), "w"), indent=1)
    return st


def report_time(url):
    """'.../Injury-Report_2026-10-27_05_30PM.pdf' -> datetime (ET wall time; used for ordering only)."""
    m = re.search(r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{1,2})(?:_(\d{2}))?(AM|PM)", url)
    if not m:
        return None
    d, h, mi, ap = m.groups()
    return dt.datetime.strptime(f"{d} {int(h)}:{mi or '00'} {ap}", "%Y-%m-%d %I:%M %p")


# ------------------------------------------------------------------ build (no network)
def build(root):
    import pandas as pd
    recs = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(root, "ledger", "*.json")))]
    if not recs:
        print("feed build: ledger is empty; nothing to build")
        return None
    bad = {r["id"]: r["problems"] for r in recs if r.get("problems")}
    rows = []
    for r in sorted(recs, key=lambda r: (r["date"], r["id"])):
        for p in r["players"]:
            rows.append({**p, "gameId": r["id"], "date": r["date"]})
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
    act = P[P.status == "ACTIVE"].sort_values(["date", "gameId"]).groupby("name").tail(1)
    act.assign(player=act.name, source_game=act.date + " " + act.gameId)[["player", "team", "source_game"]] \
        .to_csv(os.path.join(root, "feed_box_seen.csv"), index=False)
    availability(root, P)
    # what was known at tip-off of each finished game: who started and who was dressed (listed active)
    ctx = {}
    for r in recs:
        ctx[f"{r['date']}_{r['away']}-at-{r['home']}"] = {
            side: {"starters": [p["name"] for p in r["players"] if p["team"] == r[side] and p["starter"]],
                   "active": [p["name"] for p in r["players"] if p["team"] == r[side] and p["status"] == "ACTIVE"]}
            for side in ("home", "away")}
    json.dump(ctx, open(os.path.join(root, "game_context.json"), "w"))
    sp = os.path.join(root, "nba_schedule.json")
    if os.path.exists(sp):   # closing line for finished games (benchmark), from the ledger
        sched = json.load(open(sp))
        close = {r["id"]: r.get("market") for r in recs if r.get("market") and r["market"].get("p_home") is not None}
        for g in sched["games"]:
            if g["id"] in close:
                g["market"] = close[g["id"]]
        json.dump(sched, open(sp, "w"))
    lu = PBP.build_lineups(root, {r["id"]: r for r in recs})
    through = max(r["date"] for r in recs)
    mp = os.path.join(root, "meta.json")
    if os.path.exists(mp):
        m = json.load(open(mp))
        m.update(stats_date=through, season_through=f"2026-27 through {dt.date.fromisoformat(through):%b %-d} (ESPN box scores)",
                 rosters_as_of=f"box scores + injury reports, {dt.date.fromisoformat(through):%b %-d}")
        json.dump(m, open(mp, "w"))
    json.dump({"through": through, "games": len(recs), "players": len(pg), "suspect_games": bad, "lineups": lu},
              open(os.path.join(root, "feed_build.json"), "w"), indent=1)
    print(f"feed build: {len(recs)} games through {through}, {len(pg)} players, {len(bad)} games failing checks")
    return through


def availability(root, P):
    """Auto roster status, weakest evidence first, each later source overriding: (1) the team's last two box scores
    (inactive for injury in both -> out, in the last only -> questionable, played last game -> active);
    (2) ESPN's injury list (Out -> out, Day-To-Day -> day-to-day), only entries newer than the team's last game;
    (3) the official NBA report (Out -> out, Doubtful -> doubtful, Questionable -> questionable, Probable/Available -> active).
    The app turns doubtful/questionable/day-to-day into a chance of playing (config: availability.p_play)."""
    import pandas as pd
    out = {}
    last_game = {}
    for team, d in P.groupby("team"):
        ids = sorted(d[["date", "gameId"]].drop_duplicates().itertuples(index=False, name=None))[-2:]
        last_game[team] = ids[-1][0]
        gids = [g for _, g in ids]
        for name, pdx in P[(P.team == team) & P.gameId.isin(gids)].groupby("name"):
            pdx = pdx.drop_duplicates("gameId").set_index("gameId").reindex(gids)
            inj = [(isinstance(r, str) and "INJURY" in r) for r in pdx.reason]
            if bool(pdx.played.iloc[-1] == True):  # noqa: E712 (reindexed column may hold NaN)
                out[name] = (team, "active", f"played {ids[-1][0]}")
            elif len(gids) == 2 and all(inj):
                out[name] = (team, "out", f"inactive last {len(gids)} games: {pdx.desc.iloc[-1] or 'injury'}")
            elif inj[-1]:
                out[name] = (team, "questionable", f"inactive {ids[-1][0]}: {pdx.desc.iloc[-1] or 'injury'}")
    ep = os.path.join(root, "espn_injuries.json")
    if os.path.exists(ep):
        for r in json.load(open(ep))["rows"]:
            if not r.get("team"):
                continue
            if r.get("date") and r["date"][:10] < last_game.get(r["team"], "") and out.get(r["player"], ("", ""))[1] == "active":
                continue   # he has played since this note was written
            s = {"Out": "out", "Day-To-Day": "day-to-day"}.get(r["status"])
            if s:
                out[r["player"]] = (r["team"], s, f"ESPN: {r['status']} ({r['reason']})" + (f", return ~{r['return']}" if r.get("return") else ""))
    rp = os.path.join(root, "injury_report.json")
    if os.path.exists(rp):
        rep = json.load(open(rp))
        for r in rep["rows"]:
            s = {"Out": "out", "Doubtful": "doubtful", "Questionable": "questionable"}.get(r["status"], "active")
            out[r["player"]] = (r["team"], s, f"NBA report {rep.get('report_time', '')[:16]}: {r['status']} — {r['reason']}")
    pd.DataFrame([{"player": k, "team": t, "status": s, "note": n} for k, (t, s, n) in sorted(out.items())],
                 columns=["player", "team", "status", "note"]).to_csv(os.path.join(root, "availability.csv"), index=False)


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
            r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", relay, tmp], capture_output=True, text=True, timeout=180)
            src = os.path.join(tmp, "data", SEASON)
            if r.returncode == 0 and os.path.isdir(os.path.join(src, "ledger")):
                os.makedirs(os.path.join(root, "ledger"), exist_ok=True)
                n = 0
                for sub in ("ledger", "pbp", "injury_reports"):
                    os.makedirs(os.path.join(root, sub), exist_ok=True)
                    for f in glob.glob(os.path.join(src, sub, "*.json")):
                        dst = os.path.join(root, sub, os.path.basename(f))
                        txt = open(f).read()
                        if not os.path.exists(dst) or open(dst).read() != txt:   # new game, or a backfilled field
                            n += sub == "ledger" and not os.path.exists(dst)
                            open(dst, "w").write(txt)
                for fn in ["nba_schedule.json", "injury_report.json", "espn_injuries.json", "feed_status.json"]:
                    if os.path.exists(os.path.join(src, fn)):
                        open(os.path.join(root, fn), "w").write(open(os.path.join(src, fn)).read())
                fs = os.path.join(root, "feed_status.json")
                st = json.load(open(fs)) if os.path.exists(fs) else {}
                st["route"] = f"GitHub relay ({relay})"
                json.dump(st, open(fs, "w"), indent=1)
                print(f"feed: relay ok, {n} new games copied (relay fetched {st.get('fetched_at', '?')[:16]})")
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
