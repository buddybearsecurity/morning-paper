#!/usr/bin/env python3
"""Morning Paper: builds a one-article daily digest and emails it to Instapaper.

Sections: Heads Up, Weather, Traffic & Construction, Local News, Cybersecurity, Sky News.
Every section is optional and driven by config.toml (see config.example.toml).
Stdlib only, plus `ephem` for Sky News and `boto3` for the optional R2 "link" delivery.

Secrets (SMTP login, Instapaper address, R2 keys) are read from environment variables only.
Never put them in config files: config is committed to the repo.

Usage:
  python morning_paper.py --dry-run          # build out/paper.html, don't send
  python morning_paper.py                    # build and send (what GitHub Actions runs)
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import ssl
import sys
import tomllib
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (MorningPaper; personal RSS digest)"
ATOM = "{http://www.w3.org/2005/Atom}"
WARNINGS: list[str] = []  # collected fetch problems, printed at the end of the paper
SOURCES: list[str] = []   # names of sources actually used, for the footer


def used(name: str) -> None:
    if name and name not in SOURCES:
        SOURCES.append(name)


# --------------------------------------------------------------------------- helpers
def fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str):
    return json.loads(fetch(url))


def safe(label: str, fn, default=None):
    """Run a section builder; never let one broken source kill the paper."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        WARNINGS.append(f"{label}: {e}")
        traceback.print_exc(file=sys.stderr)
        return default


def strip_html(s: str | None, limit: int = 260) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


@dataclass
class Item:
    source: str
    title: str
    link: str
    summary: str = ""
    published: datetime | None = None
    score: int = 0
    reasons: list[str] = field(default_factory=list)


def parse_feed(raw: bytes, source: str) -> list[Item]:
    root = ET.fromstring(raw)
    items: list[Item] = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        for it in root.iter("item"):
            items.append(Item(
                source=source,
                title=strip_html(it.findtext("title"), 300),
                link=(it.findtext("link") or "").strip(),
                summary=strip_html(it.findtext("description")),
                published=parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")),
            ))
    else:  # Atom
        for it in root.iter(f"{ATOM}entry"):
            link = ""
            for l in it.findall(f"{ATOM}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            items.append(Item(
                source=source,
                title=strip_html(it.findtext(f"{ATOM}title"), 300),
                link=link,
                summary=strip_html(it.findtext(f"{ATOM}summary") or it.findtext(f"{ATOM}content")),
                published=parse_date(it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated")),
            ))
    return items


def recent(items: list[Item], hours: int, now: datetime) -> list[Item]:
    cutoff = now - timedelta(hours=hours)
    return [i for i in items if i.published is None or i.published >= cutoff]


def get_feed(name: str, url: str) -> list[Item]:
    items = parse_feed(fetch(url), name)
    used(name)
    return items


def compass(az_deg: float) -> str:
    pts = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return pts[int((az_deg % 360) / 45 + 0.5) % 8]


def hhmm(d: datetime | None) -> str:
    return d.strftime("%-I:%M %p").lower() if d else "—"


# --------------------------------------------------------------------------- weather
WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def units(cfg) -> dict:
    if cfg["general"].get("units", "metric") == "imperial":
        return {"t": "°F", "w": "mph", "p": "in", "om": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                                                       "precipitation_unit": "inch"}}
    return {"t": "°C", "w": "km/h", "p": "mm", "om": {}}


def open_meteo(cfg) -> dict:
    loc = cfg["location"]
    q = urllib.parse.urlencode({
        **units(cfg)["om"],
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": cfg["general"]["timezone"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m",
        "hourly": "cloud_cover,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
                 "weather_code,wind_speed_10m_max,wind_gusts_10m_max,uv_index_max",
        "forecast_days": 3,
    })
    data = fetch_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    used("Open-Meteo")
    return data


def build_weather(cfg, om: dict | None) -> str:
    out = []
    u = units(cfg)
    # Optional Environment Canada feed (Canada only): warnings + forecast text
    ec_items: list[Item] = []
    for url in cfg.get("weather", {}).get("ec_rss", []):
        try:
            ec_items = get_feed("Environment Canada", url)
            if ec_items:
                break
        except Exception as e:  # noqa: BLE001
            WARNINGS.append(f"Environment Canada ({url}): {e}")
    warn = [i for i in ec_items if re.search(r"warning|watch|statement|advisory", i.title, re.I)
            and not re.search(r"no watches or warnings", i.title, re.I)]
    for w in warn:
        out.append(f"<p><strong>⚠ {esc(w.title)}</strong><br>{esc(w.summary)}</p>")

    if om:
        c, d = om["current"], om["daily"]
        out.append(
            f"<p><strong>Now:</strong> {c['temperature_2m']:.0f}{u['t']} (feels {c['apparent_temperature']:.0f}{u['t']}), "
            f"{WMO.get(c['weather_code'], 'mixed')}, wind {c['wind_speed_10m']:.0f} {u['w']} {compass(c['wind_direction_10m'])}.</p>")
        amt = d["precipitation_sum"][0] or 0
        amount = (f" ({amt:.0f} mm)" if amt >= 1 else "") if u["p"] == "mm" else (f" ({amt:.1f} in)" if amt >= 0.05 else "")
        out.append(
            f"<p><strong>Today:</strong> high {d['temperature_2m_max'][0]:.0f}{u['t']}, low {d['temperature_2m_min'][0]:.0f}{u['t']}, "
            f"{WMO.get(d['weather_code'][0], 'mixed')}. "
            f"{d['precipitation_probability_max'][0] or 0}% chance of precipitation{amount}. "
            f"Gusts to {d['wind_gusts_10m_max'][0]:.0f} {u['w']}. UV {d['uv_index_max'][0] or 0:.0f}.</p>")
        out.append(
            f"<p><strong>Tomorrow:</strong> {d['temperature_2m_max'][1]:.0f}° / {d['temperature_2m_min'][1]:.0f}{u['t']}, "
            f"{WMO.get(d['weather_code'][1], 'mixed')}, {d['precipitation_probability_max'][1] or 0}% POP.</p>")

    fc = [i for i in ec_items if ":" in i.title and not re.search(r"current conditions|warning|watch|statement", i.title, re.I)]
    if fc:
        out.append("<p><em>Environment Canada says:</em></p><ul>")
        for i in fc[:3]:
            out.append(f"<li>{esc(i.summary or i.title)}</li>")
        out.append("</ul>")
    if not out:
        return "<p>Weather sources were unavailable this morning.</p>"
    return "\n".join(out)


# --------------------------------------------------------------------------- traffic
def index_feed_urls(page_url: str, labels: list[str]) -> dict[str, str]:
    """Find RSS links on an index page by the label text next to them (e.g. a city's 'RSS feeds' page)."""
    page = fetch(page_url).decode("utf-8", "replace")
    found = {}
    for label in labels:
        pos = page.find(label)
        if pos < 0:
            continue
        m = re.search(r'href="([^"]+)"', page[pos:pos + 1500])
        if m:
            found[label] = urllib.parse.urljoin(page_url, html.unescape(m.group(1)))
    return found


ARCGIS_DEFAULT_FIELDS = {  # matches Halifax's Street_Closures layer; override per city in config
    "street": "STREET_NAME", "type": "CLOSURE_TYPE", "start": "START_DATE", "end": "END_DATE",
    "start_time": "START_TIME", "end_time": "END_TIME", "comments": "COMMENTS",
    "reopen_evenings": "REOPEN_EVENINGS", "stage": "CLOSURE_STAGE",
}


def arcgis_closures(ac: dict, today: date) -> list[str]:
    """Active closures today from an ArcGIS FeatureServer layer (many cities publish one)."""
    layer = ac["layer"]
    f = {**ARCGIS_DEFAULT_FIELDS, **ac.get("fields", {})}
    meta = fetch_json(f"{layer}?f=json")
    domains = {}
    for fld in meta.get("fields", []):
        dom = fld.get("domain") or {}
        if dom.get("codedValues"):
            domains[fld["name"]] = {str(cv["code"]): cv["name"] for cv in dom["codedValues"]}
    d = today.isoformat()
    where = f"{f['start']} <= DATE '{d}' AND {f['end']} >= DATE '{d}'"
    for stage in ac.get("exclude_stages", []):
        where += f" AND {f['stage']} <> '{stage.replace(chr(39), chr(39) * 2)}'"
    q = urllib.parse.urlencode({"where": where, "outFields": "*", "returnGeometry": "false", "f": "json",
                                "orderByFields": f"{f['start']} DESC", "resultRecordCount": 200})
    data = fetch_json(f"{layer}/query?{q}")
    if "error" in data:
        raise RuntimeError(data["error"])
    used(ac.get("name", "ArcGIS closures"))
    order = {t: n for n, t in enumerate(ac.get("type_order", []))}
    rows = []
    for feat in data.get("features", []):
        a = feat["attributes"]
        name = lambda k: domains.get(f[k], {}).get(str(a.get(f[k])), a.get(f[k]))  # noqa: E731
        street = name("street") or "Unnamed street"
        ctype = name("type") or "Closure"
        end = datetime.fromtimestamp(a[f["end"]] / 1000, tz=timezone.utc).date() if a.get(f["end"]) else None
        times = f"{a.get(f['start_time']) or ''}–{a.get(f['end_time']) or ''}".strip("–")
        extra = []
        if times:
            extra.append(times)
        if a.get(f["reopen_evenings"]) == "Yes":
            extra.append("reopens evenings")
        if end:
            extra.append(f"until {end.strftime('%b %-d')}")
        note = strip_html(a.get(f["comments"]), 140)
        rows.append((order.get(ctype, len(order)), street,
                     f"<li><strong>{esc(street)}</strong> — {esc(ctype)}"
                     f"{' (' + esc(', '.join(extra)) + ')' if extra else ''}"
                     f"{'. ' + esc(note) if note else ''}</li>"))
    rows.sort(key=lambda r: (r[0], r[1]))
    seen, lines = set(), []
    for _, street, line in rows:  # one line per street
        if street in seen:
            continue
        seen.add(street)
        lines.append(line)
    return lines


def build_traffic(cfg, now: datetime, today: date) -> str | None:
    t = cfg.get("traffic")
    if not t:
        return None
    out = []
    max_items = t.get("max_items", 8)
    kw = [k.lower() for k in t.get("area_keywords", [])]

    feeds = list(t.get("feeds", []))
    # Feeds discovered on an index page by label
    ip = t.get("index_page")
    if ip:
        urls = safe(f"{ip.get('name', 'RSS index')}", lambda: index_feed_urls(ip["url"], ip["labels"]), {}) or {}
        feeds += [{"name": label, "url": url, "lookback_hours": ip.get("lookback_hours", 48)} for label, url in urls.items()]

    for fd in feeds:
        items = safe(fd["name"], lambda x=fd: recent(get_feed(x["name"], x["url"]), x.get("lookback_hours", 48), now), []) or []
        if fd.get("filter_by_area") and kw:
            items = [i for i in items if any(k in f"{i.title} {i.summary}".lower() for k in kw)]
        if items:
            out.append(f"<p><strong>{esc(fd.get('heading', fd['name']))}</strong></p><ul>")
            for i in items[: fd.get("max_items", max_items)]:
                out.append(f'<li><a href="{esc(i.link)}">{esc(i.title)}</a>'
                           f"{' — ' + esc(i.summary) if i.summary else ''}</li>")
            out.append("</ul>")

    ac = t.get("arcgis_closures")
    if ac:
        lines = safe(ac.get("name", "Street closures"), lambda: arcgis_closures(ac, today), []) or []
        if lines:
            out.append(f"<p><strong>Active street closures today</strong> ({len(lines)})</p><ul>")
            out.extend(lines[:max_items])
            if len(lines) > max_items:
                out.append(f"<li>…and {len(lines) - max_items} more.</li>")
            out.append("</ul>")

    return "\n".join(out) or "<p>No closures or advisories found for today. Enjoy it while it lasts.</p>"


# --------------------------------------------------------------------------- news
def item_li(i: Item, show_source: bool = False) -> str:
    src = f" <em>({esc(i.source)})</em>" if show_source else ""
    summ = f"<br>{esc(i.summary)}" if i.summary and i.summary.lower() != i.title.lower() else ""
    return f'<li><a href="{esc(i.link)}"><strong>{esc(i.title)}</strong></a>{src}{summ}</li>'


def build_local(cfg, now: datetime) -> str | None:
    if not cfg.get("local_news"):
        return None
    hours = cfg["general"]["lookback_hours"]
    out = []
    for src in cfg["local_news"]:
        items = safe(src["name"], lambda s=src: recent(get_feed(s["name"], s["url"]), hours, now), []) or []
        if not items:
            continue
        out.append(f"<h3>{esc(src['name'])}</h3><ul>")
        out.extend(item_li(i) for i in items[: src.get("max_items", 3)])
        out.append("</ul>")
    return "\n".join(out) or "<p>No local stories came through this morning.</p>"


STOP = set("the a an and or of to in on for with from by at as is are was were be has have after over into new "
           "its it this that how why what says said amid more than".split())


def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", s.lower()) if w not in STOP and len(w) > 2}


def build_cyber(cfg, now: datetime) -> tuple[str, str]:
    """Returns (heads_up_html, cyber_html)."""
    hours = cfg["general"]["lookback_hours"]
    hu = cfg.get("heads_up", {})
    watch = [w.lower() for w in hu.get("watchlist", [])]
    big = [w.lower() for w in hu.get("big_vendors", [])]
    alarms = [a.lower() for a in hu.get("alarm_phrases", [])]

    by_source: dict[str, list[Item]] = {}
    for src in cfg.get("cyber_news", []):
        items = safe(src["name"], lambda s=src: recent(get_feed(s["name"], s["url"]), hours, now), []) or []
        by_source[src["name"]] = items[: max(src.get("max_items", 5) * 3, 10)]

    mainstream: list[Item] = []
    mwords = [w.lower() for w in hu.get("mainstream_cyber_words", [])]
    for src in cfg.get("mainstream", []):
        items = safe(src["name"], lambda s=src: recent(get_feed(s["name"], s["url"]), hours, now), []) or []
        mainstream += [i for i in items if any(w in i.title.lower() for w in mwords)]

    # Score each item
    all_items = [i for v in by_source.values() for i in v] + mainstream
    for i in all_items:
        text = f"{i.title} {i.summary}".lower()
        hits = [a for a in alarms if a in text]
        if hits:
            i.score += min(len(hits), 3)
            i.reasons.append(", ".join(hits[:3]))
        wl = [w for w in watch if w in text]
        if wl:
            i.score += 3
            i.reasons.append("watchlist: " + ", ".join(sorted(set(wl))))
        bv = [w for w in big if re.search(rf"\b{re.escape(w)}\b", text)]
        if bv and not wl:
            i.score += 1
        if i in mainstream:
            i.score += 2
            i.reasons.append(f"made {i.source}")

    # Cluster the same story across sources (shared CVE or similar headline)
    clusters: list[list[Item]] = []
    for i in sorted(all_items, key=lambda x: -x.score):
        cves = set(re.findall(r"CVE-\d{4}-\d+", f"{i.title} {i.summary}", re.I))
        ti = tokens(i.title)
        for c in clusters:
            head = c[0]
            hc = set(re.findall(r"CVE-\d{4}-\d+", f"{head.title} {head.summary}", re.I))
            th = tokens(head.title)
            jac = len(ti & th) / max(1, len(ti | th))
            if (cves & hc) or jac >= 0.3:
                c.append(i)
                break
        else:
            clusters.append([i])

    flagged = []
    for c in clusters:
        sources = {x.source for x in c}
        score = max(x.score for x in c) + 2 * (len(sources) - 1)
        if score >= hu.get("score_threshold", 3):
            flagged.append((score, c, sources))
    flagged.sort(key=lambda t: -t[0])
    flagged = flagged[:6]
    flagged_ids = {id(x) for _, c, _ in flagged for x in c}

    # CISA KEV additions (last 2 days)
    kev_lines = []
    def kev():
        if not hu.get("cisa_kev"):
            return
        data = fetch_json(hu["cisa_kev"])
        used("CISA KEV")
        cutoff = (now - timedelta(days=2)).date().isoformat()
        for v in data.get("vulnerabilities", []):
            if v.get("dateAdded", "") >= cutoff:
                txt = f"{v.get('vendorProject')} {v.get('product')}".lower()
                star = "★ " if any(w in txt for w in watch) else ""
                kev_lines.append(
                    f"<li>{star}<strong>{esc(v.get('vendorProject'))} {esc(v.get('product'))}</strong> — "
                    f"{esc(v.get('vulnerabilityName'))} ({esc(v.get('cveID'))}). "
                    f"Federal patch deadline {esc(v.get('dueDate'))}"
                    f"{'; ransomware use known' if v.get('knownRansomwareCampaignUse') == 'Known' else ''}.</li>")
    safe("CISA KEV", kev)

    hu_out = []
    if flagged:
        hu_out.append("<ul>")
        for score, c, sources in flagged:
            lead = max(c, key=lambda x: (x.source not in [m.source for m in mainstream], x.score))
            reasons = sorted({r for x in c for r in x.reasons})
            others = ", ".join(sorted(sources - {lead.source}))
            hu_out.append(
                f'<li><a href="{esc(lead.link)}"><strong>{esc(lead.title)}</strong></a> <em>({esc(lead.source)})</em>'
                f"{'<br>' + esc(lead.summary) if lead.summary else ''}"
                f"<br><small>Why it's here: {esc('; '.join(reasons))}"
                f"{'. Also covered by ' + esc(others) if others else ''}.</small></li>")
        hu_out.append("</ul>")
    if kev_lines:
        hu_out.append(f"<p><strong>Added to CISA's Known Exploited Vulnerabilities list</strong></p><ul>{''.join(kev_lines)}</ul>")
    if not hu_out:
        hu_out.append("<p>Quiet morning. Nothing flagged as day-ruining (yet).</p>")

    cy_out = []
    for src in cfg.get("cyber_news", []):
        rest = [i for i in by_source.get(src["name"], []) if id(i) not in flagged_ids][: src.get("max_items", 5)]
        if rest:
            cy_out.append(f"<h3>{esc(src['name'])}</h3><ul>")
            cy_out.extend(item_li(i) for i in rest)
            cy_out.append("</ul>")
    return "\n".join(hu_out), "\n".join(cy_out) or "<p>No cyber feeds came through.</p>"


# --------------------------------------------------------------------------- sky
SHOWERS = [  # (name, month, day, ZHR, note)
    ("Quadrantids", 1, 3, 110, "best before dawn, radiant in the NE"),
    ("Lyrids", 4, 22, 18, "best after midnight"),
    ("Eta Aquariids", 5, 6, 40, "low from our latitude; pre-dawn only"),
    ("Southern Delta Aquariids", 7, 30, 20, "low in the south, pre-dawn"),
    ("Perseids", 8, 12, 100, "best after midnight, radiant in the NE"),
    ("Draconids", 10, 8, 10, "early evening, usually modest"),
    ("Orionids", 10, 21, 20, "best after midnight"),
    ("Southern Taurids", 11, 5, 5, "slow, bright fireballs possible"),
    ("Northern Taurids", 11, 12, 5, "slow, bright fireballs possible"),
    ("Leonids", 11, 17, 15, "best before dawn"),
    ("Geminids", 12, 14, 150, "the year's best; good from mid-evening"),
    ("Ursids", 12, 22, 10, "radiant near the Little Dipper"),
]


def build_sky(cfg, today: date, tz: ZoneInfo, om: dict | None) -> str:
    try:
        import ephem  # type: ignore
    except ImportError:
        return "<p>(Sky News needs the <code>ephem</code> package.)</p>"
    sky_defaults = {"aurora_kp_watch": 5, "aurora_kp_good": 6, "min_planet_alt_deg": 10,
                    "iss_tle": "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"}
    loc, sky = cfg["location"], {**sky_defaults, **cfg.get("sky", {})}

    def obs_at(dt_local: datetime):
        o = ephem.Observer()
        o.lat, o.lon, o.elevation = str(loc["lat"]), str(loc["lon"]), loc.get("elevation_m", 0)
        o.date = ephem.Date(dt_local.astimezone(timezone.utc).replace(tzinfo=None))
        return o

    def to_local(edate) -> datetime:
        return ephem.Date(edate).datetime().replace(tzinfo=timezone.utc).astimezone(tz)

    midnight = datetime(today.year, today.month, today.day, tzinfo=tz)
    o = obs_at(midnight)
    sun = ephem.Sun()
    sunrise, sunset = to_local(o.next_rising(sun)), to_local(o.next_setting(sun))
    o.horizon = "-6"
    dusk = to_local(o.next_setting(sun, use_center=True))
    o.horizon = "0"
    day_len = sunset - sunrise
    o_y = obs_at(midnight - timedelta(days=1))
    y_len = to_local(o_y.next_setting(sun)) - to_local(o_y.next_rising(sun))
    delta = int((day_len - y_len).total_seconds() / 60)
    lines = [f"<p><strong>Sun:</strong> rises {hhmm(sunrise)}, sets {hhmm(sunset)} "
             f"({day_len.seconds // 3600}h {day_len.seconds % 3600 // 60}m of daylight, "
             f"{'+' if delta >= 0 else '−'}{abs(delta)} min vs yesterday). Dark enough for stars by {hhmm(dusk)}.</p>"]

    # Moon
    noon = obs_at(midnight + timedelta(hours=12))
    moon = ephem.Moon(noon)
    illum = moon.phase
    nn, nf = ephem.next_new_moon(noon.date), ephem.next_full_moon(noon.date)
    waxing = nf < nn
    if illum < 3:
        phase = "New moon"
    elif illum > 97:
        phase = "Full moon"
    elif 45 <= illum <= 55:
        phase = "First quarter" if waxing else "Last quarter"
    else:
        phase = ("Waxing " if waxing else "Waning ") + ("crescent" if illum < 50 else "gibbous")
    mo = obs_at(midnight + timedelta(hours=12))
    mr, ms = to_local(mo.next_rising(ephem.Moon())), to_local(mo.next_setting(ephem.Moon()))
    nxt = ("Full moon " + to_local(nf).strftime("%a %b %-d")) if waxing else ("New moon " + to_local(nn).strftime("%a %b %-d"))
    lines.append(f"<p><strong>Moon:</strong> {phase}, {illum:.0f}% lit. Rises {hhmm(mr)}, sets {hhmm(ms)}. Next: {nxt}.</p>")

    # Planets — evening (1h after sunset) and morning (1h before tomorrow's sunrise)
    def visible(when: datetime):
        ob = obs_at(when)
        res = []
        for name, body in [("Mercury", ephem.Mercury()), ("Venus", ephem.Venus()), ("Mars", ephem.Mars()),
                           ("Jupiter", ephem.Jupiter()), ("Saturn", ephem.Saturn())]:
            body.compute(ob)
            alt = math.degrees(body.alt)
            if alt >= sky["min_planet_alt_deg"]:
                res.append(f"{name} ({compass(math.degrees(body.az))}, {alt:.0f}° up, mag {body.mag:.1f})")
        return res

    ev = visible(sunset + timedelta(hours=1))
    o2 = obs_at(midnight + timedelta(days=1))
    morn = visible(to_local(o2.next_rising(sun)) - timedelta(hours=1))
    if ev:
        lines.append(f"<p><strong>Planets this evening</strong> ({hhmm(sunset + timedelta(hours=1))}): {esc(', '.join(ev))}.</p>")
    if morn:
        lines.append(f"<p><strong>Planets before dawn tomorrow:</strong> {esc(', '.join(morn))}.</p>")

    # Meteor showers within 3 days of peak
    for name, m, d, zhr, note in SHOWERS:
        peak = date(today.year, m, d)
        gap = (peak - today).days
        if -2 <= gap <= 3:
            when = "peaks tonight" if gap == 0 else (f"peaks in {gap} day{'s' if gap > 1 else ''}" if gap > 0 else "just past peak")
            moon_note = " Moonlight will wash out the fainter ones." if illum > 60 else " Moon's not much of a problem."
            lines.append(f"<p><strong>Meteors:</strong> the {name} {when} (up to ~{zhr}/hr under dark skies; {note}).{moon_note}</p>")

    # Aurora (NOAA SWPC Kp forecast)
    def aurora():
        data = fetch_json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json")
        used("NOAA SWPC")
        rows = []
        if data and isinstance(data[0], list):  # older array-of-arrays format
            hdr = data[0]
            rows = [dict(zip(hdr, r)) for r in data[1:]]
        else:
            rows = data
        start, end = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=30)
        best = None
        for r in rows:
            t = datetime.fromisoformat(str(r["time_tag"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
            if start <= t <= end and str(r.get("observed")) in ("predicted", "estimated"):
                kp = float(r["kp"])
                if best is None or kp > best[0]:
                    best = (kp, t)
        if not best:
            return None
        kp, t = best
        when = t.astimezone(tz).strftime("%a %-I %p").lower()
        if kp >= sky["aurora_kp_good"]:
            return (f"<p><strong>Aurora: worth a look.</strong> Kp forecast peaks at {kp:.1f} around {when}. "
                    f"Get away from city light and look low to the north.</p>")
        if kp >= sky["aurora_kp_watch"]:
            return (f"<p><strong>Aurora: long shot.</strong> Kp up to {kp:.1f} around {when}; maybe a faint glow "
                    f"low on the northern horizon (cameras will see it before your eyes do).</p>")
        return f"<p><strong>Aurora:</strong> unlikely (max Kp {kp:.1f}).</p>"
    a = safe("NOAA aurora", aurora)
    if a:
        lines.append(a)

    # ISS visible passes in the next ~24h
    def iss():
        tle = [l.strip() for l in fetch(sky["iss_tle"]).decode().splitlines() if l.strip()]
        sat = ephem.readtle(tle[0], tle[1], tle[2])
        used("CelesTrak")
        ob = obs_at(datetime.now(tz))
        ob.horizon = "10"
        stop = ephem.Date(ob.date + 1.1)
        passes = []
        while ob.date < stop and len(passes) < 4:
            try:
                tr, azr, tt, altt, ts, azs = ob.next_pass(sat)
            except ValueError:
                break
            if tr is None or ts is None:
                break
            ob.date = tt
            sat.compute(ob)
            s = ephem.Sun(); s.compute(ob)
            if not sat.eclipsed and math.degrees(s.alt) < -6:
                passes.append(f"{to_local(tr).strftime('%a')} {hhmm(to_local(tr))} ({compass(math.degrees(azr))} → {compass(math.degrees(azs))}, "
                              f"max {math.degrees(altt):.0f}°)")
            ob.date = ephem.Date(ts + ephem.minute)
        return passes
    p = safe("ISS passes", iss, []) or []
    if p:
        lines.append(f"<p><strong>ISS visible passes:</strong> {esc('; '.join(p))}.</p>")

    # Tonight's cloud cover for observing (Open-Meteo hourly, 9 pm – 2 am)
    if om:
        try:
            times = om["hourly"]["time"]
            cc = om["hourly"]["cloud_cover"]
            tonight = [cc[k] for k, t in enumerate(times)
                       if t.startswith(today.isoformat()) and int(t[11:13]) >= 21
                       or t.startswith((today + timedelta(days=1)).isoformat()) and int(t[11:13]) <= 2]
            if tonight:
                avg = sum(tonight) / len(tonight)
                verdict = "clear-ish, good for looking up" if avg < 30 else "patchy cloud" if avg < 70 else "mostly cloudy, probably a bust"
                lines.append(f"<p><strong>Tonight's sky:</strong> ~{avg:.0f}% cloud cover, 9 pm–2 am ({verdict}).</p>")
        except Exception as e:  # noqa: BLE001
            WARNINGS.append(f"cloud cover: {e}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- assemble + send
SECTION_TITLES = {"heads_up": "Heads Up", "weather": "Weather", "traffic": "Traffic &amp; Construction",
                  "local": "Local News", "cyber": "Cybersecurity", "sky": "Sky News"}


def build_paper(cfg) -> tuple[str, str]:
    tz = ZoneInfo(cfg["general"]["timezone"])
    now_local = datetime.now(tz)
    now = now_local.astimezone(timezone.utc)
    today = now_local.date()
    title = f"{cfg['general']['title_prefix']}: {now_local.strftime('%A, %B %-d, %Y')}"
    order = cfg["general"].get("sections", list(SECTION_TITLES))

    om = safe("Open-Meteo", lambda: open_meteo(cfg)) if {"weather", "sky"} & set(order) else None
    parts: dict[str, str | None] = {}
    if "weather" in order:
        parts["weather"] = build_weather(cfg, om)
    if "traffic" in order:
        parts["traffic"] = build_traffic(cfg, now, today)
    if "local" in order:
        parts["local"] = build_local(cfg, now)
    if {"heads_up", "cyber"} & set(order) and (cfg.get("cyber_news") or cfg.get("heads_up")):
        parts["heads_up"], parts["cyber"] = safe("Cyber", lambda: build_cyber(cfg, now), ("<p>Cyber section failed.</p>", ""))
    if "sky" in order:
        parts["sky"] = safe("Sky", lambda: build_sky(cfg, today, tz, om), "<p>Sky section failed.</p>")

    sections = "\n\n".join(f"<h2>{SECTION_TITLES[k]}</h2>\n{parts[k]}" for k in order if parts.get(k))
    skipped = (" Skipped this morning: " + esc("; ".join(w.split(":")[0] for w in WARNINGS))) if WARNINGS else ""
    body = f"""<html><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title></head><body>
<h1>{esc(title)}</h1>
<p><em>{esc(cfg['location']['name'])} · built {now_local.strftime('%-I:%M %p').lower()}</em></p>

{sections}

<hr>
<p><small>Sources: {esc(', '.join(SOURCES) or 'none')}.{skipped}</small></p>
</body></html>"""
    return title, body


KOBO_CSS = """
<style>
  /* Sized for a Kobo Clara Colour (6", ~91 x 122 mm), so pages fill the screen without zooming */
  @page { size: 90mm 120mm; margin: 5mm 5mm 6mm 5mm; }
  html { -webkit-print-color-adjust: exact; }
  body { font-family: Georgia, "Times New Roman", serif; font-size: 10.5pt; line-height: 1.35; color: #000; margin: 0; }
  h1 { font-size: 15pt; margin: 0 0 2mm; line-height: 1.15; }
  h2 { font-size: 13pt; margin: 4mm 0 1.5mm; padding-bottom: 0.8mm; border-bottom: 0.4pt solid #000;
       break-after: avoid; page-break-after: avoid; }
  h3 { font-size: 11pt; margin: 3mm 0 1mm; break-after: avoid; page-break-after: avoid; }
  p { margin: 0 0 2mm; }
  ul { margin: 0 0 2mm; padding-left: 4mm; }
  li { margin-bottom: 1.6mm; break-inside: avoid; page-break-inside: avoid; }
  a { color: #000; text-decoration: none; }
  small { font-size: 8.5pt; }
  hr { border: 0; border-top: 0.4pt solid #000; }
</style>
"""

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN") or "", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def make_pdf(body: str) -> bytes:
    """Print the paper to a Kobo-sized PDF with headless Chrome (preinstalled on GitHub's Ubuntu runners)."""
    chrome = next((c for c in CHROME_CANDIDATES if c and (shutil.which(c) or os.path.exists(c))), None)
    if not chrome:
        raise RuntimeError("no Chrome/Chromium found for PDF rendering (set CHROME_BIN)")
    styled = body.replace("</head>", KOBO_CSS + "</head>", 1)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "paper.html", Path(tmp) / "paper.pdf"
        src.write_text(styled, encoding="utf-8")
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
               "--hide-scrollbars", f"--print-to-pdf={out}", src.as_uri()]
        subprocess.run(cmd, check=True, timeout=120, capture_output=True)
        data = out.read_bytes()
    if not data.startswith(b"%PDF"):
        raise RuntimeError("Chrome didn't produce a PDF")
    return data


def publish(title: str, body: str, today: str) -> str | None:
    """Upload the paper to a Cloudflare R2 bucket at an unguessable path; return its public URL.
    Returns None (and the caller falls back to an HTML email) if R2 isn't configured."""
    need = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "PAPER_BASE_URL"]
    if not all(os.environ.get(k) for k in need):
        return None
    import secrets
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )
    key = f"p/{today}-{secrets.token_urlsafe(16)}.html"
    s3.put_object(Bucket=os.environ["R2_BUCKET"], Key=key, Body=body.encode("utf-8"),
                  ContentType="text/html; charset=utf-8", CacheControl="private, max-age=86400")
    url = f"{os.environ['PAPER_BASE_URL'].rstrip('/')}/{key}"
    # make sure it's actually reachable before handing it to Instapaper
    for _ in range(5):
        try:
            if fetch(url, timeout=15):
                return url
        except Exception:  # noqa: BLE001
            pass
        import time
        time.sleep(3)
    raise RuntimeError(f"uploaded but not publicly reachable: {url}")


def send(title: str, body: str, url: str | None = None, pdf: bytes | None = None) -> None:
    user, pw, to = os.environ["SMTP_USER"], os.environ["SMTP_PASS"], os.environ["INSTAPAPER_EMAIL"]
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    sender = os.environ.get("SMTP_FROM") or user  # From address, if it differs from the login name
    if pdf:
        # PDF attachment: Instapaper keeps it as a document, and those do sync to Kobo
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(f"{title}\n", "plain", "utf-8"))
        part = MIMEApplication(pdf, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=f"{title.replace(':', ' -')}.pdf")
        msg.attach(part)
    elif url:
        # Link-only email: Instapaper saves the web page, which syncs to Kobo like any other article
        msg = MIMEText(f"{url}\n", "plain", "us-ascii")  # 7bit, so the bare URL is visible to Instapaper's parser
    else:
        # Fallback: full paper in the email (readable on the web, but Kobo skips these)
        msg = MIMEMultipart("alternative")
        plain = re.sub(r"\n{3,}", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", body)))
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(body, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = title, sender, to
    ctx = ssl.create_default_context()
    if port == 465:  # implicit TLS
        conn = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
    else:            # 587 (or 25): STARTTLS, refuse to continue in plaintext
        conn = smtplib.SMTP(host, port, timeout=60)
        conn.ehlo()
        if not conn.has_extn("starttls"):
            raise RuntimeError(f"{host}:{port} doesn't offer STARTTLS; refusing to send credentials in the clear")
        conn.starttls(context=ctx)
        conn.ehlo()
    with conn as s:
        s.login(user, pw)
        s.sendmail(sender, [to], msg.as_string())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write out/paper.html instead of sending")
    ap.add_argument("--config", default=None, help="defaults to config.toml, else config.example.toml")
    args = ap.parse_args()
    cfg_path = Path(args.config) if args.config else next(
        p for p in (HERE / "config.toml", HERE / "config.example.toml") if p.exists())
    cfg = tomllib.loads(cfg_path.read_text())
    print(f"Config: {cfg_path.name}")

    title, body = build_paper(cfg)
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    (out / "paper.html").write_text(body, encoding="utf-8")
    print(f"Built: {title}")
    for w in WARNINGS:
        print(f"  warning: {w}")
    today = datetime.now(ZoneInfo(cfg["general"]["timezone"])).date().isoformat()
    mode = os.environ.get("DELIVERY") or cfg["general"].get("delivery", "html")
    url = pdf = None
    if mode == "pdf":
        try:
            pdf = make_pdf(body)
            (out / "paper.pdf").write_bytes(pdf)
        except Exception as e:  # noqa: BLE001
            print(f"  warning: PDF render failed, falling back: {e}")
    if not args.dry_run and (mode == "link" or (mode == "pdf" and pdf is None)):
        try:
            url = publish(title, body, today)
        except Exception as e:  # noqa: BLE001
            print(f"  warning: publish to R2 failed, falling back to HTML email: {e}")
    if args.dry_run:
        print(f"Dry run: {out / 'paper.html'}{' + paper.pdf' if pdf else ''}")
        return 0
    send(title, body, url, pdf)
    how = "PDF attachment" if pdf else ("link" if url else ("HTML email" if mode == "html" else "HTML email (fallback)"))
    print(f"Sent to Instapaper as {how}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
