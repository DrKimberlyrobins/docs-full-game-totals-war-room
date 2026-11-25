import json, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
tz=ZoneInfo("America/Chicago")
now=datetime.now(tz)
url="https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50&limit=1000&dates="+now.strftime("%Y%m%d")
with urllib.request.urlopen(url,timeout=45) as response:
    raw=json.load(response)
games=[]
for event in raw["events"]:
    comp=event["competitions"][0]
    teams={x["homeAway"]:x for x in comp["competitors"]}
    if not {"home","away"} <= teams.keys(): continue
    start=datetime.fromisoformat(event["date"].replace("Z","+00:00")).astimezone(tz)
    if start.date()!=now.date(): continue
    final=None
    if comp["status"]["type"]["completed"] and comp["status"]["type"]["name"]=="STATUS_FINAL":
        final=sum(int(x["score"]) for x in teams.values())
    games.append(dict(id=event["id"],time=start.strftime("%I:%M %p"),away=teams["away"]["team"]["displayName"],home=teams["home"]["team"]["displayName"],final=final,start=start.isoformat()))
games.sort(key=lambda x:x["start"])
Path("data.json").write_text(json.dumps(dict(date=now.date().isoformat(),updated=now.isoformat(),games=games)))
