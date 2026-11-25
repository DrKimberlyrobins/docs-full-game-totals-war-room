"""Build the public board from timestamped, independently sourced evidence."""
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
CBB = "https://drkimberlyrobins.github.io/2026-docs-ncaab-war-room/data"
RAW = "https://raw.githubusercontent.com/DrKimberlyrobins/2026-docs-ncaab-war-room/main/data"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KAMBI = "https://eu.offering-api.kambicdn.com/offering/v2018/pivusmsrl-bil"
KAMBI_QUERY = "?lang=en_US&market=US-MS&client_id=200&channel_id=7"
ARCHIVE = Path("archive")
REVIEWED = json.loads(Path(__file__).with_name("team-aliases.json").read_text())


def now():
    return datetime.now(timezone.utc)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch(url, token=None):
    headers = {"Accept": "application/json", "User-Agent": "Docs-Totals-Research/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers=headers), timeout=12) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt:
                raise


def upstream(path):
    try:
        return fetch(f"{CBB}/{path}")
    except OSError:
        return fetch(f"{RAW}/{path}")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    temp.replace(path)


def norm(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", value)


def names(team):
    return {norm(team[key]) for key in ("location", "displayName", "shortDisplayName", "abbreviation") if team.get(key)}


def scoreboard(day):
    return ESPN + "/scoreboard?groups=50&limit=1000&dates=" + day.replace("-", "")


def game_rows(events, day):
    for event in events:
        start = instant(event["date"])
        if str(start.astimezone(CENTRAL).date()) != day:
            continue
        comp = event["competitions"][0]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        if not {"away", "home"} <= sides.keys():
            continue
        state = comp["status"]["type"]
        final = state["name"].startswith("STATUS_FINAL") and state["completed"]
        yield {
            "id": str(event["id"]), "start": event["date"], "neutral": comp.get("neutralSite"),
            "venue": comp.get("venue", {}).get("fullName"), "state": state["state"],
            "status": state["description"], "statusCode": state["name"],
            "teams": [sides[k]["team"] for k in ("away", "home")],
            "scores": [int(sides[k]["score"]) for k in ("away", "home")]
            if final and all(str(sides[k].get("score", "")).isdigit() for k in ("away", "home")) else None,
        }


def tempo(value):
    for minimum, label in [(74, "Very Fast"), (71, "Fast"), (67, "Average"), (64, "Slow")]:
        if value >= minimum:
            return label
    return "Very Slow"


def category(gap):
    for minimum, label in [(8, "Very Large"), (6, "Large"), (4, "Moderate"), (2, "Small")]:
        if abs(gap) >= minimum:
            return label
    return "Minimal"


def baseline(inputs):
    a, b = inputs
    possessions = (a["tempo"] + b["tempo"]) / 2
    efficiencies = [(a["offense"] + b["defense"]) / 2, (b["offense"] + a["defense"]) / 2]
    scores = [possessions * value / 100 for value in efficiencies]
    return dict(version="average-pace-efficiency-v1", possessions=possessions, scores=scores,
                total=sum(scores), efficiencies=efficiencies)


def ratings_api(day):
    token = os.getenv("KENPOM_API_KEY")
    if not (token and os.getenv("KENPOM_PUBLICATION_AUTHORIZED") == "true"):
        return None
    season = int(day[:4]) + int(int(day[5:7]) >= 7)
    rows = fetch(f"https://kenpom.com/api.php?endpoint=ratings&y={season}", token)
    ratings = {}
    for row in rows:
        values = dict(tempo=float(row["AdjTempo"]), offense=float(row["AdjOE"]),
                      defense=float(row["AdjDE"]), margin=float(row["AdjEM"]),
                      rank=int(row["RankAdjEM"]), record=f'{row["Wins"]}-{row["Losses"]}', through=row.get("DataThrough"))
        if not all(math.isfinite(v) for v in (values["tempo"], values["offense"], values["defense"], values["margin"])):
            raise ValueError("Nonfinite KenPom rating")
        key = row["TeamName"]
        if key in ratings:
            raise ValueError("Duplicate KenPom team")
        ratings[key] = values
    if len(ratings) < 300:
        raise ValueError("Incomplete KenPom ratings")
    return ratings, now().isoformat()


def projection(game, prepared, api):
    candidate = prepared.get(game["id"], {})
    teams = game["teams"]
    ids = [str(team["id"]) for team in teams]
    if api:
        ratings, stamp = api
        inputs = []
        for team in teams:
            reviewed = REVIEWED.get(str(team["id"]))
            matched = [value for key, value in ratings.items()
                       if key == reviewed or (reviewed is None and norm(key) in names(team))]
            if len(matched) != 1:
                break
            inputs.append(dict(matched[0], classification=tempo(matched[0]["tempo"])))
        if len(inputs) == 2:
            model = baseline(inputs)
            model.update(inputs=inputs, tempoGap=abs(inputs[0]["tempo"] - inputs[1]["tempo"]),
                         source="https://kenpom.com/api-documentation.php", ratingsAt=stamp)
            return model
    row, source = candidate if candidate else ({}, {})
    model = row.get("model")
    if not model or [row.get("awayId"), row.get("homeId")] != ids:
        return None
    if abs((instant(row["date"]) - instant(game["start"])).total_seconds()) > 900:
        return None
    return dict(version="cbb-kenpom-additive-v1", possessions=model["possessions"],
                scores=[model["away"], model["home"]], total=model["total"],
                efficiencies=[model["awayEfficiency"], model["homeEfficiency"]],
                source=source["url"], ratingsAt=source["ratingsFetchedAt"], inputs=None,
                evidence={"matchedTeams": row.get("matchedTeams"), "settings": source.get("config")})


def kalshi_markets(day, games):
    by_game = defaultdict(list)
    cursor = ""
    stamp = now().isoformat()
    for _ in range(12):
        url = KALSHI + "/events?series_ticker=KXNCAAMBTOTAL&status=open&limit=100&with_nested_markets=true"
        payload = fetch(url + ("&cursor=" + cursor if cursor else ""))
        for event in payload["events"]:
            expected = instant(day + "T12:00:00+00:00").strftime("%y%b%d").upper()
            if not event["event_ticker"].startswith("KXNCAAMBTOTAL-" + expected):
                continue
            title = re.fullmatch(r"(.+) vs\.? (.+): Point Total", event["title"], re.I)
            if not title:
                continue
            first, second = map(norm, title.groups())
            matches = [g for g in games if any(first in names(a) and second in names(b)
                                               for a, b in (g["teams"], g["teams"][::-1]))]
            if len(matches) != 1:
                continue
            for market in event.get("markets", []):
                rules = market.get("rules_primary", "")
                line = market.get("floor_strike")
                if (market.get("status") != "active" or market.get("strike_type") != "greater"
                        or not isinstance(line, (int, float)) or not 0 < line < 400
                        or not market.get("title", "").startswith("Full Game:")
                        or not re.search(r"over " + re.escape(f"{line:g}") + r" total points", rules, re.I)
                        or "men's college basketball" not in rules.lower()):
                    continue
                by_game[matches[0]["id"]].append(dict(
                    source="Kalshi", line=line, yes=market.get("yes_ask_dollars"), no=market.get("no_ask_dollars"),
                    ticker=market["ticker"], timestamp=market.get("updated_time") or stamp,
                    retrievedAt=stamp, settlement="strict-greater", rules=rules,
                    secondaryRules=market.get("rules_secondary", "")))
        cursor = payload.get("cursor")
        if not cursor:
            return by_game
    raise ValueError("Kalshi event pagination incomplete")


def kambi_detail(event):
    url = f"{KAMBI}/betoffer/event/{event['id']}.json{KAMBI_QUERY}&includeParticipants=true"
    matches = []
    for offer in fetch(url)["betOffers"]:
        criterion = offer.get("criterion", {})
        tags = offer.get("tags", [])
        if (str(criterion.get("id")) != "1001159509" or criterion.get("label") != "Total Points - Including Overtime"
                or str(offer.get("eventId")) != str(event["id"]) or "OFFERED_PREMATCH" not in tags):
            continue
        rows = offer.get("outcomes", [])
        if len(rows) != 2 or any(x.get("status") != "OPEN" or str(x.get("betOfferId")) != str(offer["id"]) for x in rows):
            continue
        sides = {x.get("label", "").lower(): x for x in rows}
        if sides.keys() != {"over", "under"}:
            continue
        over, under = sides["over"], sides["under"]
        raw = over.get("line")
        if type(raw) is not int or not 0 < raw < 400000 or raw != under.get("line"):
            continue
        prices = [int(x["oddsAmerican"]) for x in (over, under)]
        if min(map(abs, prices)) < 100:
            continue
        matches.append(dict(
            source="Kambi", line=raw / 1000, over=prices[0], under=prices[1], sourceUrl=url,
            timestamp=now().isoformat(), settlement="push-on-equal", offerId=str(offer["id"]),
            main=bool(set(tags) & {"MAIN", "MAIN_LINE"})))
    preferred = [m for m in matches if m["main"]] or matches
    return preferred[0] if len(preferred) == 1 else None


def kambi_markets(games):
    url = KAMBI + "/listView/basketball/ncaab/all/all/matches.json" + KAMBI_QUERY + "&useCombined=true&useCombinedLive=true"
    candidates = defaultdict(list)
    for row in fetch(url)["events"]:
        event = row.get("event", {})
        if (event.get("sport"), event.get("group"), event.get("state")) != ("BASKETBALL", "NCAAB", "NOT_STARTED"):
            continue
        first, second = (norm(event.get(k, "")) for k in ("awayName", "homeName"))
        matches = [g for g in games if g["state"] == "pre"
                   and abs((instant(g["start"]) - instant(event["start"])).total_seconds()) <= 900
                   and any(first in names(a) and second in names(b) for a, b in (g["teams"], g["teams"][::-1]))]
        if len(matches) == 1:
            candidates[matches[0]["id"]].append(event)
    result, errors = {}, 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {gid: pool.submit(kambi_detail, events[0]) for gid, events in candidates.items() if len(events) == 1}
        for gid, job in jobs.items():
            try:
                item = job.result()
                if item:
                    result[gid] = [item]
            except (OSError, ValueError, KeyError):
                errors += 1
    return result, errors


def compare(markets, model):
    total = model["total"] if model else None
    for market in markets:
        if total is not None:
            gap = round(total - market["line"], 9)
            market.update(gap=gap, category=category(gap), direction="OVER" if gap > 0 else "UNDER" if gap < 0 else "EVEN")
    return sorted(markets, key=lambda row: (row["source"], row["line"]))


def grade_market(market, actual):
    direction = market.get("direction")
    if direction not in {"OVER", "UNDER"}:
        return None
    if actual == market["line"] and market["settlement"] == "push-on-equal":
        return "Push"
    winning = "OVER" if actual > market["line"] else "UNDER"
    return "Win" if winning == direction else "Loss"


def receipt(game, stamp):
    model = game["model"]
    if (not model or game["state"] != "pre" or game["statusCode"] not in {"STATUS_SCHEDULED"}
            or instant(stamp) >= instant(game["start"])
            or not timedelta(0) <= instant(stamp) - instant(model["ratingsAt"]) <= timedelta(hours=24)):
        return None
    return dict(id=game["id"], start=game["start"], savedAt=stamp, teams=[t["displayName"] for t in game["teams"]],
                model=model, markets=receipt_markets(game["markets"], stamp))


def receipt_markets(markets, stamp):
    fresh = [m for m in markets if timedelta(0) <=
             instant(stamp) - instant(m.get("retrievedAt", m["timestamp"])) <= timedelta(minutes=30)]
    # One representative Kalshi threshold per game avoids counting correlated alternates as separate picks.
    def available(price):
        try:
            return 0 < float(price) < 1
        except (ValueError, TypeError):
            return False
    kalshi = [m for m in fresh if m["source"] == "Kalshi" and available(m.get("yes")) and available(m.get("no"))]
    selected = [m for m in fresh if m["source"] == "Kambi"]
    if kalshi:
        selected.append(min(kalshi, key=lambda m: (abs(float(m["yes"]) - float(m["no"])), m["line"])))
    return selected


def load_receipt(path):
    original = json.loads(path.read_text())
    extensions = (ARCHIVE / "quotes").glob(path.stem + "-*.json")
    return dict(original, markets=original["markets"] + [json.loads(file.read_text()) for file in sorted(extensions)])


def save_later_quotes(game, stamp, captured):
    if game["state"] != "pre" or game["statusCode"] != "STATUS_SCHEDULED" or instant(stamp) >= min(
            instant(game["start"]), instant(captured["start"])):
        return
    saved_sources = {market["source"] for market in captured["markets"]}
    new_quotes = [market.copy() for market in receipt_markets(game["markets"], stamp)
                  if market["source"] not in saved_sources]
    for market in compare(new_quotes, captured["model"]):
        save(ARCHIVE / "quotes" / f'{game["id"]}-{market["source"]}.json', dict(market, savedAt=stamp))


def archive_results(games_by_id, schedule_status):
    rows = []
    for path in sorted((ARCHIVE / "receipts").glob("*.json")):
        captured = load_receipt(path)
        final_path = ARCHIVE / "results" / path.name
        final = json.loads(final_path.read_text()) if final_path.exists() else None
        current = games_by_id.get(captured["id"])
        if current and current["scores"]:
            scores = current["scores"]
            if final is None or final["scores"] != scores:
                final = dict(scores=scores, total=sum(scores), checkedAt=now().isoformat(), source=schedule_status)
                save(final_path, final)
        result = None
        if final:
            error = captured["model"]["total"] - final["total"]
            result = dict(final, error=error, absoluteError=abs(error),
                          grades=[dict(market, outcome=grade_market(market, final["total"])) for market in captured["markets"]])
        rows.append(dict(receipt=captured, result=result))
    return rows


def summarize(rows):
    groups = defaultdict(lambda: {"games": set(), "errors": [], "outcomes": Counter()})
    for row in rows:
        final = row["result"]
        if not final:
            continue
        saved = row["receipt"]
        keys = ["All games", "Model · " + saved["model"]["version"]]
        keys += ["Tempo · " + label for label in {t["classification"] for t in saved["model"]["inputs"] or []}]
        for key in keys:
            groups[key]["games"].add(saved["id"])
            groups[key]["errors"].append(final["absoluteError"])
        for market in final["grades"]:
            if not market["outcome"]:
                continue
            source = market["source"]
            for key in [source, source + " · " + market["direction"], source + " · " + market["category"]]:
                groups[key]["games"].add(saved["id"])
                groups[key]["outcomes"][market["outcome"]] += 1
    return {key: dict(games=len(group["games"]), mae=sum(group["errors"]) / len(group["errors"]) if group["errors"] else None,
                      outcomes=group["outcomes"]) for key, group in sorted(groups.items())}


def main():
    day = str(now().astimezone(CENTRAL).date())
    source = scoreboard(day)
    games = sorted(game_rows(fetch(source)["events"], day), key=lambda g: instant(g["start"]))
    health = {}
    jobs = {
        "CBB ratings-based projections": lambda: upstream(f"calculator/daily/{day}.json"),
        "KenPom direct ratings": lambda: ratings_api(day),
        "Kalshi": lambda: kalshi_markets(day, games),
        "Kambi": lambda: kambi_markets(games),
    }
    values = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {label: pool.submit(job) for label, job in jobs.items()}
        for label, future in futures.items():
            try:
                values[label] = future.result()
                health[label] = "Retrieved" if values[label] is not None else "Public ratings permission and API access not configured"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                health[label] = f"Unavailable ({type(exc).__name__})"
    prepared = values.get("CBB ratings-based projections") or {}
    prepared["url"] = f"{CBB}/calculator/daily/{day}.json"
    by_id = {str(row["id"]): (row, prepared) for row in prepared.get("matchups", [])} if prepared.get("date") == day else {}
    api = values.get("KenPom direct ratings")
    kalshi = values.get("Kalshi", {})
    kambi, errors = values.get("Kambi", ({}, 0))
    if errors:
        health["Kambi"] = f"Partial · {errors} event requests failed"
    stamp = now().isoformat()
    for game in games:
        gid = game["id"]
        game["model"] = projection(game, by_id, api)
        if game["model"]:
            model = game["model"]
            model["stale"] = instant(stamp) - instant(model["ratingsAt"]) > timedelta(hours=24)
            model["environment"] = "High scoring" if model["total"] >= 150 else "Low scoring" if model["total"] < 135 else "Midrange scoring"
        game["markets"] = compare(kalshi.get(gid, []) + kambi.get(gid, []), game["model"] if not (game["model"] or {}).get("stale") else None)
        path = ARCHIVE / "receipts" / f"{gid}.json"
        if not path.exists():
            item = receipt(game, stamp)
            if item:
                save(path, item)
        elif game["state"] == "pre" and instant(stamp) < instant(game["start"]):
            save_later_quotes(game, stamp, load_receipt(path))
        if path.exists() and (game["state"] != "pre" or instant(stamp) >= instant(game["start"])):
            frozen = load_receipt(path)
            game["model"] = frozen["model"]
            game["markets"] = frozen["markets"]
            game["frozen"] = True
        game["receipt"] = path.exists()
    results_games = {g["id"]: g for g in games}
    pending = defaultdict(set)
    for path in (ARCHIVE / "receipts").glob("*.json"):
        if not (ARCHIVE / "results" / path.name).exists() and path.stem not in results_games:
            saved = json.loads(path.read_text())
            past = str(instant(saved["start"]).astimezone(CENTRAL).date())
            pending[past].add(path.stem)
    for past, ids in pending.items():
        try:
            results_games.update({g["id"]: g for g in game_rows(fetch(scoreboard(past))["events"], past) if g["id"] in ids})
        except (OSError, ValueError, KeyError):
            health[f"Final scores {past}"] = "Unavailable · will retry"
    rows = archive_results(results_games, ESPN)
    distribution = Counter(tempo(t["tempo"]) for t in api[0].values()) if api else None
    research = json.loads(Path(__file__).with_name("research.json").read_text())
    save(Path("data.json"), dict(research=research, date=day, updated=stamp, source=source, health=health, games=games,
                                distribution=distribution, review=summarize(rows), archive=rows))
    print(json.dumps(dict(date=day, games=len(games), projections=sum(bool(g["model"]) for g in games),
                          kambi=len(kambi), kalshi=len(kalshi), receipts=len(rows), health=health)))


if __name__ == "__main__":
    main()
