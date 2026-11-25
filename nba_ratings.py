"""Estimate NBA pace and efficiencies from completed ESPN team box scores."""

import json
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from statistics import mean, median, quantiles

LOOKBACK_DAYS = 18
MAX_GAMES = 10
MIN_GAMES = 5
LABELS = ("Very Slow", "Slow", "Average", "Fast", "Very Fast")


def box_sample(payload):
    comp = payload["header"]["competitions"][0]
    if not comp["status"]["type"]["name"].startswith("STATUS_FINAL"):
        raise ValueError("Box score is not final")
    points = {str(team["id"]): int(team["score"]) for team in comp["competitors"]}
    possessions = {}
    for team in payload["boxscore"]["teams"]:
        stats = {row["name"]: row["displayValue"] for row in team["statistics"]}
        if not {"fieldGoalsMade-fieldGoalsAttempted", "freeThrowsMade-freeThrowsAttempted",
                "offensiveRebounds", "totalTurnovers"} <= stats.keys():
            raise ValueError("Incomplete team box score")
        fga = int(stats["fieldGoalsMade-fieldGoalsAttempted"].split("-")[-1])
        fta = int(stats["freeThrowsMade-freeThrowsAttempted"].split("-")[-1])
        possessions[str(team["team"]["id"])] = fga + .44 * fta - int(stats["offensiveRebounds"]) + int(stats["totalTurnovers"])
    if len(points) != 2 or points.keys() != possessions.keys() or any(not 50 <= p <= 220 for p in possessions.values()):
        raise ValueError("Invalid box score team mapping or possessions")
    period = int(comp["status"].get("period") or comp.get("period") or 4)
    minutes = 48 + 5 * max(0, period - 4)
    return dict(teams=points, possessions=mean(possessions.values()), minutes=minutes)


def recent_boxes(day, cache, espn, fetch, scoreboard, instant, now, save):
    results = {}
    errors = 0
    days = [(day - timedelta(days=i)).isoformat() for i in range(1, LOOKBACK_DAYS + 1)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        schedules = {date: pool.submit(fetch, scoreboard(date, espn)) for date in days}
        requested = {}
        for date, job in schedules.items():
            path = cache / (date + ".json")
            if path.exists():
                results.update(json.loads(path.read_text()))
            try:
                events = job.result()["events"]
            except (OSError, ValueError, KeyError):
                errors += 1
                continue
            for event in events:
                comp = event["competitions"][0]
                if instant(event["date"]) >= now() or not comp["status"]["type"]["name"].startswith("STATUS_FINAL"):
                    continue
                gid = str(event["id"])
                if gid not in results and gid not in requested:
                    url = espn + "/summary?event=" + gid
                    requested[gid] = (date, event, pool.submit(fetch, url))
        new = defaultdict(dict)
        for gid, (date, event, job) in requested.items():
            try:
                sample = box_sample(job.result())
                comp = event["competitions"][0]
                expected = {str(side["team"]["id"]): int(side["score"]) for side in comp["competitors"]}
                if sample["teams"] != expected:
                    raise ValueError("Box score does not match scoreboard")
                # Use the scoreboard period to normalize pace after overtime.
                sample["minutes"] = 48 + 5 * max(0, int(comp["status"].get("period", 4)) - 4)
                results[gid] = new[date][gid] = dict(sample, tip=event["date"], date=date, id=gid)
            except (OSError, ValueError, TypeError, KeyError):
                errors += 1
        for date in new:
            save(cache / (date + ".json"), {gid: box for gid, box in results.items() if box["date"] == date})
    return results, errors


def profiles(boxes):
    team_games = defaultdict(list)
    for game in sorted(boxes, key=lambda row: row["tip"], reverse=True):
        for team, points in game["teams"].items():
            opponent, = (score for tid, score in game["teams"].items() if tid != team)
            team_games[team].append((game, points, opponent))
    ratings = {}
    for team, history in team_games.items():
        last = history[:MAX_GAMES]
        if len(last) < MIN_GAMES:
            continue
        possessions = sum(game["possessions"] for game, _, _ in last)
        pace = possessions * 48 / sum(game["minutes"] for game, _, _ in last)
        offense = 100 * sum(points for _, points, _ in last) / possessions
        defense = 100 * sum(points for _, _, points in last) / possessions
        ratings[team] = dict(tempo=pace, offense=offense, defense=defense, margin=offense-defense,
                             sample=len(last), through=last[0][0]["date"],
                             sampleGames=[game["id"] for game, _, _ in last if "id" in game])
    if len(ratings) >= 10:
        deciles = quantiles([r["tempo"] for r in ratings.values()], n=10, method="inclusive")
        cuts = [deciles[i] for i in (0, 2, 6, 8)]
        for rank, team in enumerate(sorted(ratings, key=lambda t: (-ratings[t]["margin"], t)), 1):
            rating = ratings[team]
            rating.update(rank=rank, classification=LABELS[bisect_right(cuts, rating["tempo"])])
    return ratings


def forecast(teams, ratings, stamp, source, baseline, boxes):
    inputs = []
    for team in teams:
        rating = ratings.get(str(team["id"]))
        if not rating or "classification" not in rating:
            return None
        inputs.append(dict(rating, record=team.get("record")))
    model = baseline(inputs)
    center = median(sum(g["teams"].values()) * 48 / g["minutes"] for g in boxes)
    total = model["total"]
    model.update(version="nba-recent-possessions-v1", inputs=inputs, source=source, ratingsAt=stamp,
                 tempoGap=abs(inputs[0]["tempo"] - inputs[1]["tempo"]),
                 environment="High scoring" if total > center+8 else "Low scoring" if total < center-8 else "Midrange scoring",
                 environmentBaseline=center)
    return model


def research(boxes, ratings, baseline, source):
    groups = defaultdict(list)
    for box in boxes:
        groups[box["date"]].append(box)
    earlier, errors, benchmark_errors = [], [], []
    for date, on_date in sorted(groups.items()):
        prior = profiles(earlier)
        for game in on_date:
            first, second = game["teams"]
            if first in prior and second in prior:
                estimated = baseline([prior[first], prior[second]])["total"]
                actual = sum(game["teams"].values())
                errors.append(abs(estimated - actual))
                benchmark_errors.append(abs(median(sum(g["teams"].values()) for g in earlier) - actual))
        earlier.extend(on_date)
    return dict(tempo=dict(source=source, counts=Counter(r["classification"] for r in ratings.values() if "classification" in r)),
                nbaHistorical=dict(games=len(errors), modelMae=mean(errors), benchmarkMae=mean(benchmark_errors),
                                   firstDate=min(groups), lastDate=max(groups), source=source) if errors else None)
