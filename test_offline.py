"""Offline smoke test: feeds every source canned data and builds the paper.
Run: python test_offline.py   (writes out/paper.html; no network or secrets needed)"""
import json
import re
from datetime import datetime, timedelta, timezone

import morning_paper as mp

NOW = datetime.now(timezone.utc)
RFC = lambda h: (NOW - timedelta(hours=h)).strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731
ISO = lambda h: (NOW - timedelta(hours=h)).isoformat()  # noqa: E731


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>https://ex.com/{i}</link><description><![CDATA[<p>{d}</p>]]></description>"
                   f"<pubDate>{RFC(h)}</pubDate></item>" for i, (t, d, h) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def atom(items):
    body = "".join(f'<entry><title>{t}</title><link rel="alternate" href="https://ex.com/a{i}"/><summary>{d}</summary>'
                   f"<updated>{ISO(h)}</updated></entry>" for i, (t, d, h) in enumerate(items))
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>{body}</feed>'.encode()


def hourly():
    start = datetime.now(mp.ZoneInfo("America/Halifax")).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72)]
    return {"time": times, "cloud_cover": [20 + (h % 5) * 5 for h in range(72)], "precipitation_probability": [10] * 72}


FIX = {
    "weather.gc.ca/rss/weather": atom([
        ("SPECIAL WEATHER STATEMENT IN EFFECT", "Heavy rain expected tonight. Rainfall amounts near 40 mm.", 2),
        ("Current Conditions: 11.2°C", "Mostly cloudy", 1),
        ("Thursday: Rain. High 16.", "Rain. Amount 15 to 25 mm. Wind south 30 km/h gusting to 60. High 16.", 1),
        ("Thursday night: Showers. Low 9.", "Showers ending after midnight then cloudy. Low 9.", 1),
    ]),
    "open-meteo": json.dumps({
        "current": {"temperature_2m": 11.2, "apparent_temperature": 8.9, "weather_code": 61, "wind_speed_10m": 24, "wind_direction_10m": 190},
        "daily": {"temperature_2m_max": [16.1, 14.0, 13], "temperature_2m_min": [9.2, 7.1, 6], "precipitation_probability_max": [90, 30, 10],
                  "precipitation_sum": [22.4, 0.4, 0], "weather_code": [63, 2, 1], "wind_speed_10m_max": [32, 20, 10],
                  "wind_gusts_10m_max": [61, 35, 20], "uv_index_max": [1.2, 3.4, 3]},
        "hourly": hourly()}).encode(),
    "rss-feeds": b'<table><tr><td>Road closures and construction</td><td><a href="/rss/news/road-closures">RSS feed</a></td></tr>'
                 b'<tr><td>Halifax Transit service disruptions</td><td><a href="https://www.halifax.ca/rss/transit-disruptions">RSS feed</a></td></tr></table>',
    "/rss/news/road-closures": rss([("Traffic Advisory: Barrington Street lane closure", "Single lane closure Oct 1-3 for utility work.", 5)]),
    "transit-disruptions": rss([("Route 10 detour", "Route 10 on detour due to road work on Wyse Road.", 3), ("Old item", "x", 200)]),
    "FeatureServer/0?f=json": json.dumps({"fields": [{"name": "STREET_NAME", "domain": {"codedValues": [{"code": "101", "name": "ROBIE ST"}, {"code": "202", "name": "QUINPOOL RD"}]}}]}).encode(),
    "FeatureServer/0/query": json.dumps({"features": [
        {"attributes": {"STREET_NAME": "101", "CLOSURE_TYPE": "Construction", "END_DATE": int((NOW + timedelta(days=9)).timestamp() * 1000), "START_TIME": "7:00", "END_TIME": "19:00", "REOPEN_EVENINGS": "Yes", "COMMENTS": "Water main replacement"}},
        {"attributes": {"STREET_NAME": "202", "CLOSURE_TYPE": "Block Party", "END_DATE": int(NOW.timestamp() * 1000), "COMMENTS": None}},
        {"attributes": {"STREET_NAME": "101", "CLOSURE_TYPE": "Utility", "END_DATE": None}},
    ]}).encode(),
    "traffic.asp": rss([("Weekly Traffic Advisories", "Halifax Regional Municipality: Highway 102 lane closure near Exit 2, Oct 1-4.", 30),
                        ("Pictou County bridge", "Bridge work in Pictou County.", 30)]),
    "rss-canada-novascotia": rss([("Halifax Water seeks rate increase", "Utility files with UARB.", 3), ("N.S. audit finds gaps", "Auditor general report.", 6), ("Old", "x", 100)]),
    "halifaxexaminer": rss([("Morning File, Thursday, October 1, 2026", "Today's news roundup.", 2)]),
    "globalnews.ca/halifax": rss([("Halifax gets $2M for violence prevention", "Federal funding announced.", 4)]),
    "haligonia": rss([("Green cart weekly collection ends October 2", "Seasonal schedule change.", 8)]),
    "bleepingcomputer": rss([("ExampleCorp patches critical Gateway flaw exploited in attacks", "CVE-2026-1234 actively exploited.", 3),
                             ("New phishing kit targets Microsoft 365", "Phishing as a service.", 5),
                             ("Linux distro releases update", "Routine release.", 7)]),
    "TheHackersNews": rss([("Attackers Exploit ExampleCorp Gateway Zero-Day CVE-2026-1234", "Zero-day exploited in the wild.", 2),
                           ("Cisco Warns of Attackers Exploiting Critical Authentication Bypass in SD-WAN Manager", "Critical vulnerability actively exploited.", 4),
                           ("Weekly recap", "Stuff happened.", 6)]),
    "cyber.gc.ca": atom([("TeamViewer security advisory (AV26-977)", "TeamViewer published an advisory.", 10)]),
    "bbci.co.uk/news/technology": rss([("Global IT outage hits airlines and banks", "A widespread outage caused by a faulty update.", 3), ("New phone launched", "x", 3)]),
    "bbci.co.uk/news/world": rss([("Summit ends", "x", 3)]),
    "rss-topstories": rss([("Canadian banks report systems down after cyberattack", "Cyberattack disrupts banking.", 4)]),
    "known_exploited": json.dumps({"vulnerabilities": [
        {"cveID": "CVE-2026-1234", "vendorProject": "ExampleCorp", "product": "Gateway", "vulnerabilityName": "ExampleCorp Gateway Command Injection",
         "dateAdded": NOW.date().isoformat(), "dueDate": (NOW.date() + timedelta(days=21)).isoformat(), "knownRansomwareCampaignUse": "Unknown"},
        {"cveID": "CVE-2020-0001", "vendorProject": "Old", "product": "Thing", "vulnerabilityName": "x", "dateAdded": "2020-01-01", "dueDate": "2020-02-01"}]}).encode(),
    "swpc": json.dumps([{"time_tag": (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"), "kp": 5.33 if h == 9 else 2.0,
                         "observed": "predicted", "noaa_scale": None} for h in range(0, 48, 3)]).encode(),
}


def fake_fetch(url, timeout=25):
    for k, v in FIX.items():
        if k in url:
            return v
    raise RuntimeError(f"no fixture for {url}")


mp.fetch = fake_fetch


def build(cfg_path, **general_overrides):
    mp.WARNINGS.clear()
    mp.SOURCES.clear()
    cfg = mp.tomllib.loads((mp.HERE / cfg_path).read_text())
    cfg["general"].update(general_overrides)
    return cfg, *mp.build_paper(cfg)


# 1. Full local example (Halifax): every section on
cfg, title, body = build("examples/halifax.toml")
(mp.HERE / "out").mkdir(exist_ok=True)
(mp.HERE / "out" / "paper.html").write_text(body)
print(title)
print("warnings:", mp.WARNINGS)
heads_up = body.split("<h2>Weather</h2>")[0]
assert "Water main replacement" in body and "ROBIE ST" in body
assert "ExampleCorp" in heads_up, "zero-day covered by two outlets should be in Heads Up"
assert "Global IT outage" in heads_up
assert "SPECIAL WEATHER STATEMENT" in body
assert "Route 10 detour" in body and "Old item" not in body
assert "Highway 102" in body and "Pictou" not in body
assert "Provincial advisories (Halifax area)" in body
assert "Halifax Examiner" in body and "Sources: " in body and "Open-Meteo" in body
assert not mp.WARNINGS, mp.WARNINGS

# 2. Generic example: no traffic/local configured, so those sections disappear
cfg2, _, body2 = build("config.example.toml", sections=["heads_up", "weather", "traffic", "local", "cyber"])
assert "<h2>Traffic" not in body2 and "<h2>Local News" not in body2
assert "<h2>Weather</h2>" in body2 and "<h2>Cybersecurity</h2>" in body2
assert "Halifax" not in body2.split("<h2>")[0]

# 3. Imperial units + a trimmed section list
_, _, body3 = build("config.example.toml", units="imperial", sections=["weather"])
assert "°F" in body3 and "mph" in body3 and "<h2>Cybersecurity" not in body3

print("OK")
