"""Play-by-play -> stints (who was on the floor, for how long, and what happened) -> 2026-27 five-man lineups.

compact_plays(summary)  ESPN game summary -> small event list kept in the ledger (data/2026-27/pbp/<id>.json)
stints(cp)              events -> stints: [period, start, end (game seconds), home five, away five, points and
                        possession counts for each side]
check(cp, st, box)      a game's stints are trusted only if they reproduce the final score and every player's
                        box-score minutes (ESPN rounds minutes to whole numbers)
Stdlib only, so the GitHub relay can run compact_plays without the rest of the project.
"""
import re

ESPN_FIX = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}
tri = lambda a: ESPN_FIX.get(a, a)


def period_len(p):
    return 720 if p <= 4 else 300


def period_start(p):
    return sum(period_len(q) for q in range(1, p))


def clock_left(s):
    """'5:53' -> 353.0 ; '57.8' -> 57.8"""
    s = str(s or "0")
    if ":" in s:
        m, sec = s.split(":")
        return int(m) * 60 + float(sec)
    return float(s)


def kind_of(play):
    t = play["type"]["text"].lower()
    if t == "substitution":
        return "sub"
    if t.startswith("free throw"):
        return "fta" if "technical" not in t else "fta_tech"
    if "offensive rebound" in t and "team" not in t:
        return "oreb"
    if "offensive team rebound" in t:
        return "oreb_team"
    if "defensive rebound" in t:
        return "dreb"
    if "turnover" in t:
        return "tov"
    if t == "end period":
        return "end"
    if play.get("shootingPlay"):
        return "fga"
    return "other"


def compact_plays(summary):
    """-> {home, away, athletes: {id: [name, team, starter]}, plays: [[seq, period, game_sec, kind, team, [ids], hs, as, made]]}"""
    comps = summary["header"]["competitions"][0]["competitors"]
    tid = {c["id"]: tri(c["team"]["abbreviation"]) for c in comps}
    side = {c["homeAway"]: tri(c["team"]["abbreviation"]) for c in comps}
    ath = {}
    for t in summary["boxscore"]["players"]:
        team = tri(t["team"]["abbreviation"])
        for a in t["statistics"][0]["athletes"]:
            ath[str(a["athlete"]["id"])] = [a["athlete"]["displayName"], team, bool(a.get("starter"))]
    plays = []
    for p in summary.get("plays", []):
        per = p["period"]["number"]
        sec = period_start(per) + period_len(per) - clock_left(p["clock"]["displayValue"])
        k = kind_of(p)
        ids = [str(x["athlete"]["id"]) for x in p.get("participants", []) if x.get("athlete", {}).get("id")]
        team = tid.get((p.get("team") or {}).get("id"))
        made = bool(p.get("scoringPlay"))
        plays.append([int(p.get("sequenceNumber") or 0), per, round(sec, 1), k, team, ids, p.get("homeScore", 0), p.get("awayScore", 0), made])
    plays.sort(key=lambda x: (x[2], x[0]))
    lines = {c["homeAway"]: [int(l.get("displayValue") or l.get("value") or 0) for l in c.get("linescores", [])] for c in comps}
    final = {c["homeAway"]: int(c.get("score") or 0) for c in comps}
    return {"home": side["home"], "away": side["away"], "athletes": ath, "plays": plays,
            "linescores": [lines.get("home", []), lines.get("away", [])], "final": [final.get("home"), final.get("away")]}


def _period_starters(cp, per, team, prev_end):
    """Five on the floor at the start of a period: players whose first appearance in the period is anything other
    than being subbed in; if fewer than five are seen, the rest stayed on from the end of the previous period."""
    first = {}
    for seq, p, sec, k, tm, ids, hs, as_, made in cp["plays"]:
        if p != per:
            continue
        for i, a in enumerate(ids):
            if cp["athletes"].get(a, [None, None])[1] != team or a in first:
                continue
            first[a] = "in" if (k == "sub" and i == 0) else "on"
    on = [a for a, v in first.items() if v == "on"]
    if len(on) < 5:
        on += [a for a in prev_end if a not in first][: 5 - len(on)]
    return set(on[:5]) if len(on) >= 5 else set(on)


def stints(cp):
    home, away = cp["home"], cp["away"]
    starters = {t: {a for a, (n, tm, s) in cp["athletes"].items() if tm == t and s} for t in (home, away)}
    periods = sorted({p[1] for p in cp["plays"]})
    out, problems = [], []
    on = {t: set(starters[t]) for t in (home, away)}
    last_score = [0, 0]
    for per in periods:
        if per > 1:
            on = {t: _period_starters(cp, per, t, on[t]) for t in (home, away)}
        else:
            for t in (home, away):
                guess = _period_starters(cp, 1, t, starters[t])
                if guess != starters[t] and len(guess) == 5:
                    problems.append(f"period 1 {t}: box starters differ from play-by-play")
        t0 = period_start(per)
        cur = _new_stint(per, t0, on, home, away, last_score)
        for seq, p, sec, k, tm, ids, hs, as_, made in [x for x in cp["plays"] if x[1] == per]:
            if k == "sub" and len(ids) == 2 and tm in on:
                if sec > cur["start"]:
                    _close(cur, sec, last_score); out.append(cur)
                    cur = _new_stint(per, sec, on, home, away, last_score)
                a_in, a_out = ids
                on[tm].discard(a_out); on[tm].add(a_in)
                cur[tm] = sorted(on[tm])
                continue
            if tm in (home, away):
                c = cur["n"][tm]
                if k == "fga":
                    c["fga"] += 1
                elif k == "fta":
                    c["fta"] += 1
                elif k in ("oreb", "oreb_team"):
                    c["oreb"] += 1
                elif k == "tov":
                    c["tov"] += 1
            if hs is not None and as_ is not None:
                last_score = [hs, as_]
        _close(cur, t0 + period_len(per), last_score); out.append(cur)
    for s in out:
        for t in (home, away):
            if len(s[t]) != 5:
                problems.append(f"period {s['period']} {t}: {len(s[t])} players on the floor at {s['start']:.0f}s")
                break
    return [s for s in out if s["end"] > s["start"]], sorted(set(problems))


def _new_stint(per, start, on, home, away, score):
    return {"period": per, "start": start, home: sorted(on[home]), away: sorted(on[away]), "score0": list(score),
            "n": {home: {"fga": 0, "fta": 0, "oreb": 0, "tov": 0}, away: {"fga": 0, "fta": 0, "oreb": 0, "tov": 0}}}


def _close(st, end, score):
    st["end"] = end
    st["pts_home"], st["pts_away"] = score[0] - st["score0"][0], score[1] - st["score0"][1]


def possessions(c):
    return c["fga"] + 0.44 * c["fta"] - c["oreb"] + c["tov"]


def valid_periods(cp, st):
    """Periods whose stint points match the official period score (play-by-play feeds occasionally scramble a
    period, e.g. duplicated overtime plays). Only these periods are used for lineup data."""
    ls_h, ls_a = cp.get("linescores", [[], []])
    ok = set()
    for per in sorted({s["period"] for s in st}):
        ph = sum(s["pts_home"] for s in st if s["period"] == per)
        pa = sum(s["pts_away"] for s in st if s["period"] == per)
        if per <= len(ls_h) and per <= len(ls_a) and (ph, pa) == (ls_h[per - 1], ls_a[per - 1]):
            ok.add(per)
    return ok


def check(cp, st, final=None, box_minutes=None, tol_min=1.6):
    """Problems list: final score not reproduced, or a player's on-floor minutes off his box-score minutes."""
    out = []
    ph, pa = sum(s["pts_home"] for s in st), sum(s["pts_away"] for s in st)
    if final and (ph, pa) != tuple(final):
        out.append(f"stint points {ph}-{pa} != final {final[0]}-{final[1]}")
    if box_minutes:
        mins = {}
        for s in st:
            for t in (cp["home"], cp["away"]):
                for a in s[t]:
                    mins[a] = mins.get(a, 0) + (s["end"] - s["start"]) / 60
        bad = []
        for a, m in box_minutes.items():
            if abs(mins.get(a, 0) - m) > tol_min:
                bad.append(f"{cp['athletes'].get(a, [a])[0]} {mins.get(a, 0):.1f} vs box {m}")
        if bad:
            out.append(f"minutes off for {len(bad)} players: " + "; ".join(bad[:4]))
    return out


def build_lineups(root, ledger):
    """pbp/*.json -> stints.csv (every stint, valid periods only) and lineups_2026_27.csv (five-man units).
    Returns a coverage summary. Minutes are checked against each game's box score when the whole game is valid."""
    import glob, json, os
    import pandas as pd
    rows, report = [], {"games": 0, "periods": 0, "periods_valid": 0, "minutes_checked": 0, "minutes_off": [], "dropped": []}
    for f in sorted(glob.glob(os.path.join(root, "pbp", "*.json"))):
        gid = os.path.basename(f)[:-5]
        cp = json.load(open(f))
        st, probs = stints(cp)
        ok = valid_periods(cp, st)
        allp = {s["period"] for s in st}
        report["games"] += 1
        report["periods"] += len(allp)
        report["periods_valid"] += len(ok)
        if allp - ok:
            report["dropped"].append(f"{gid}: periods {sorted(allp - ok)} don't match the official score")
        rec = ledger.get(gid)
        if rec and ok == allp:   # whole game valid: compare minutes with the box score
            box = {str(p["id"]): p["min"] for p in rec["players"] if p.get("id") and p["played"]}
            bad = check(cp, st, box_minutes=box)
            report["minutes_checked"] += 1
            if bad:
                report["minutes_off"].append(f"{gid}: {bad[0][:120]}")
        names = {a: v[0] for a, v in cp["athletes"].items()}
        date = rec["date"] if rec else ""
        for s in st:
            if s["period"] not in ok or len(s[cp["home"]]) != 5 or len(s[cp["away"]]) != 5:
                continue
            for team, opp, pf, pa in ((cp["home"], cp["away"], s["pts_home"], s["pts_away"]), (cp["away"], cp["home"], s["pts_away"], s["pts_home"])):
                rows.append({"game": gid, "date": date, "period": s["period"], "team": team, "opp": opp,
                             "players": " | ".join(sorted(names.get(a, a) for a in s[team])),
                             "opp_players": " | ".join(sorted(names.get(a, a) for a in s[opp])),
                             "sec": s["end"] - s["start"], "pts_for": pf, "pts_against": pa,
                             "poss_for": possessions(s["n"][team]), "poss_against": possessions(s["n"][opp])})
    S = pd.DataFrame(rows)
    if S.empty:
        return report
    S.to_csv(os.path.join(root, "stints.csv"), index=False)
    L = S.groupby(["team", "players"]).agg(games=("game", "nunique"), sec=("sec", "sum"), pts_for=("pts_for", "sum"),
                                         pts_against=("pts_against", "sum"), poss_for=("poss_for", "sum"),
                                         poss_against=("poss_against", "sum")).reset_index()
    L["minutes"] = (L.sec / 60).round(1)
    poss = ((L.poss_for + L.poss_against) / 2).clip(lower=1)
    L["poss"] = poss.round(1)
    L["off_rtg"] = (100 * L.pts_for / poss).round(1)
    L["def_rtg"] = (100 * L.pts_against / poss).round(1)
    L["net_rtg"] = (L.off_rtg - L.def_rtg).round(1)
    L = L.drop(columns=["sec"]).sort_values("minutes", ascending=False)
    L.to_csv(os.path.join(root, "lineups_2026_27.csv"), index=False)
    report.update(stints=len(S) // 2, lineups=len(L), lineup_minutes=round(float(L.minutes.sum()) / 2, 1))
    return report
