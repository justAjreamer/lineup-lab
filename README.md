# Lineup Lab data relay

A public repo whose only job is to fetch NBA data on a schedule (GitHub Actions has open internet) and commit it.
Sources: ESPN's public API (schedule, results, box scores, injury list) and the official NBA injury report.
NBA.com's own data CDN refuses GitHub's servers (403), so it is not used. Lineup Lab clones it each run (`config.json` → `feed.relay_repo`).

Contents: `nba_feed.py` (copied from `lineuplab/nba_feed.py`), `.github/workflows/nba-data.yml`, and `data/2026-27/`
(written by the workflow): `ledger/<gameId>.json` per completed game, `nba_schedule.json`, `espn_injuries.json`,
`injury_report.json`, `feed_status.json`, `relay_log.txt` (the last run's output).

Setup (one time): create an empty **public** repo, add these files, push, then open the repo's **Actions** tab and
run "NBA data relay" once by hand. Nothing here is secret; it holds public box scores only. If a run fails,
GitHub emails you, and Lineup Lab keeps using the last good data.
