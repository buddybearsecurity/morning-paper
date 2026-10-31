# Morning Paper

A daily digest emailed to your Instapaper account each morning: weather, cyber news, a "my day is gonna suck" check, sky notes, plus optional local news and traffic. It runs on GitHub Actions and free data sources.

> **Disclaimer:** This was built with AI (Claude) as a personal project and tested to "good enough", not to production standards. Errors can exist: feeds change, scrapers break, and the Heads Up scoring is a heuristic, not threat intelligence. Read the code before you trust it with your accounts, and use at your own risk.

## Before you start: keep your secrets out of the repo

- **Secrets live only in GitHub Actions secrets**: your SMTP login and password, your Instapaper address, and any API keys. GitHub never copies them into templates or forks.
- **`config.toml` is committed**, so it must never hold any of those. It's for feeds, coordinates and keywords only.
- **Make your copy private.** On a public repo, Actions run logs are public. The workflow already skips saving paper copies on public repos, but a private repo keeps everything out of view.
- **Use this template; don't fork it.** *Use this template* gives you a fresh repo with no shared history.
- Leave GitHub's secret scanning and push protection on: Settings → Code security.

## Quick start

1. Click **Use this template → Create a new repository** and choose **Private**.
2. Copy `config.example.toml` to `config.toml`, then set your `timezone`, `location` and `watchlist` (and `units`, if you want imperial). Commit it.
3. Set your send time in `.github/workflows/morning-paper.yml`. The comments there list the UTC times for common Canadian timezones.
4. Get your Instapaper save-by-email address from instapaper.com → Settings.
5. Get an SMTP login that can send mail. A Gmail app password works (myaccount.google.com/apppasswords, needs 2-Step Verification). A dedicated sending account is a good idea, so a leaked password can't read your real inbox.
6. In your repo: Settings → Secrets and variables → Actions → **New repository secret**:

```
SMTP_USER        your sending address (or login)
SMTP_PASS        app password
INSTAPAPER_EMAIL your ...@instapaper.com address
```

Not using Gmail? Also add these:

```
SMTP_HOST        e.g. smtp.fastmail.com
SMTP_PORT        587
SMTP_FROM        the From address, if it differs from SMTP_USER
```

7. Actions tab → **Morning Paper → Run workflow**. Within a couple of minutes, it's in your Instapaper inbox.

## Scheduling

The workflow runs once a day on GitHub's scheduler. GitHub cron uses UTC and ignores daylight saving, so the paper arrives an hour later in summer.

GitHub's scheduler is also best-effort: runs can start hours late on busy mornings. That's fine if you read it whenever it shows up. If you want it on time, use the optional Cloudflare Worker in `scheduler/`:

1. Create a GitHub fine-grained token: Settings → Developer settings → Fine-grained tokens. Choose *Only select repositories* → your Morning Paper repo, with **Actions: Read and write** and nothing else. Give it a 1-year expiry and set yourself a reminder to renew it.
2. Cloudflare → Workers & Pages → **Create** → Hello World. Paste in `scheduler/cloudflare-worker.js` and deploy.
3. Add the Worker variables and the two cron triggers listed at the top of that file.
4. Delete the `schedule:` block in the workflow, or you'll get two papers a day.

GitHub pauses scheduled workflows after 60 days with no repo activity (it emails you first). The Worker route doesn't have that problem.

## Make it local

`examples/halifax.toml` is a complete local setup to copy from:

- **Local news:** any site's RSS feed (`[[local_news]]`).
- **Traffic:** RSS feeds from your city or province (`[[traffic.feeds]]`), with `filter_by_area` to keep only items mentioning your `area_keywords`.
- **City feeds page:** if your city lists its RSS feeds on one page, `[traffic.index_page]` finds the links by the label text next to them.
- **Street closures:** many cities publish closures as an ArcGIS layer (search "[your city] open data street closures"). Point `[traffic.arcgis_closures]` at the layer and map its field names under `fields` if they differ from Halifax's.
- **Weather in Canada:** add Environment Canada's RSS feed for official warnings (`[weather] ec_rss`).

Anything you leave out is skipped, and `sections` in `[general]` sets which sections appear and in what order.

To preview without sending:

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python morning_paper.py --config examples/halifax.toml --dry-run
```

## How Heads Up decides

Each cyber story gets a score. At `score_threshold` (default 3) or above, it goes to the top with a "Why it's here" line.

| Signal | Points |
|---|---|
| Alarm phrase (zero-day, actively exploited, outage, …) | +1 each, max 3 |
| Your `watchlist` vendors/products | +3 |
| `big_vendors` (Microsoft, Cisco, …) | +1 |
| Made a mainstream feed (BBC, etc.) | +2 |
| Each extra outlet covering the same story or CVE | +2 |

New entries on CISA's Known Exploited Vulnerabilities list are always listed, with a ★ if they match your watchlist.

## Delivery (`delivery` in config)

- `html` (default): the paper is the email body. Links work, and it reads well in the Instapaper app and on the web. Kobo e-readers skip emailed articles, so it won't show up there.
- `pdf`: a Kobo-sized PDF (90 × 120 mm) attached to the email. It shows up on a Kobo, but links and layout can break in odd ways. Edit `@page` in `KOBO_CSS` in `morning_paper.py` to change the page size.
- `link`: uploads the page to a Cloudflare R2 bucket and emails Instapaper just the link. It syncs to a Kobo and keeps links, but needs the `R2_*` and `PAPER_BASE_URL` secrets.

If `pdf` or `link` fails, it falls back to `html`, so something always arrives.

## Tests

`python test_offline.py` runs everything against canned data, with no network and no secrets needed. It also runs on every push, via `.github/workflows/test.yml`.
