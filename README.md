# BUPT Dorm Electricity Monitor

> Get an email **before** your dorm runs out of electricity credit.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3.x-black)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-2-brightgreen)

A small self-hosted service that watches a BUPT dormitory electricity meter, performs real meter reads on a schedule, and sends email alerts when the balance drops below a threshold. It comes with a local web console for viewing the balance, the usage history, and for editing every setting from the browser.

Built for the Shahe campus electricity portal (`shgyfk.bupt.edu.cn`), which caps manual meter reads at **10 per day** — the whole design is shaped around staying well under that limit.

---

## Features

- **Scheduled meter reads** — forces a real reading every N hours (default 12) instead of relying on the portal's cached value
- **Cheap balance polling** — reads the cached balance every N minutes (default 60) without spending the daily read quota
- **Threshold alerts with cooldown** — emails once when the balance drops below your threshold, then at most once per cooldown period (default 24 h)
- **Recovery notification** — a second email when the balance climbs back above the threshold
- **Failure alerts** — notifies you after 3 consecutive query or meter-read failures
- **Web console** — live balance, remaining-days bar, balance history chart, editable settings, dorm picker
- **Dorm picker** — drill down through campus → area → building → floor → room; the choice is saved as the default
- **Zero extra infrastructure** — the scheduler runs inside the web service, so there is no cron job or Windows task to install
- **LAN access** — optional, so you can check the balance from your phone on the same Wi-Fi

---

## How it works

The portal's frontend is protected by a JavaScript anti-bot layer, which blocks naive scraping — but its JSON API is reachable directly. Authentication uses the **public query account embedded in the site's own frontend bundle**, so no student ID or password is involved.

Three endpoints do the work:

| Purpose | Cost |
| --- | --- |
| Read the cached balance | free, unlimited |
| Force a real meter read (async task + polling) | 1 unit of the daily 10 |
| Walk the building tree (for the room picker) | free |

A meter read is asynchronous: the service submits a task, receives a task ID, then polls every 2 seconds for up to 30 seconds until the meter reports back.

---

## Requirements

- Python 3.10+
- Two packages: `requests`, `flask`
- An SMTP account that allows app passwords (QQ Mail, 163, Gmail, Outlook, …)

---

## Getting started

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install -r requirements.txt
python server.py
```

On first run, `config.example.json` is copied to `config.json`. Open the console, pick your dorm, fill in your SMTP details, and save.

**Windows shortcut:** double-click `start.bat` — it launches the server and opens the browser.

```
==============================================
 Electricity monitor started
   Local        http://127.0.0.1:5000
   LAN          http://192.168.1.23:5000
   (use the LAN address from your phone on the same Wi-Fi)
==============================================
```

> **The service must stay running.** Scheduling and alerting live inside this process. Close the window and monitoring stops.

---

## Web console

| Section | What it does |
| --- | --- |
| Balance | Large readout, plus estimated days remaining and the alert threshold. Turns red below the threshold, orange under 3 days |
| Refresh meter | Performs a real meter read immediately. **Consumes 1 daily read.** |
| Daily quota | Remaining manual reads out of 10; resets at midnight |
| Next meter read | Countdown to the next forced read |
| Next balance check | Countdown to the next cached read (free) |
| History | Balance chart plus a detailed table |
| Dorm | Drill-down picker; saved as the default |
| Alerts | Threshold, both intervals, cooldown, SMTP settings |

The footer reports scheduler health, e.g. *"scheduler running · last 12:10 balance 58.90 (cached)"*.

---

## Configuration

All settings live in `config.json`, which is shared by the web console and the CLI. Edit them in the browser — changes apply on the scheduler's next tick, no restart needed.

| Setting | Range | Default |
| --- | --- | --- |
| `threshold_yuan` | > 0 | `10` |
| `site.check_interval_minutes` | 10 – 1440 | `60` |
| `refresh.interval_hours` | 1 – 24 | `12` |
| `refresh.enabled` | `true` / `false` | `true` |
| `alert.cooldown_hours` | 1 – 720 | `24` |
| `web.host` | `127.0.0.1` or `0.0.0.0` | `127.0.0.1` |

### The two intervals are not the same thing

This is the easiest part to get wrong.

**Balance check interval (default 60 min)** reads the portal's cached balance. It never touches the meter, so it costs nothing and can be set aggressively.

**Meter read interval (default 12 h)** sends a real read command to the meter. It returns a live value and **consumes 1 of the 10 daily reads**.

The key consequence: **between two meter reads the balance is frozen** — the portal does not refresh its own cache. So:

- Worst-case alert latency ≈ the *meter read* interval, not the balance check interval
- Don't set the threshold right at the point where you'd lose power. At 10 yuan and a 12-hour read interval, a balance can fall below the threshold and not be noticed for 12 hours
- Shorter reads are more responsive, but 10 reads/day caps you at one every 2.4 hours. At 12 hours you use 2 per day, leaving headroom for manual checks

### Email alerts

| Situation | Email sent? |
| --- | --- |
| Balance drops below threshold | Yes, then cooldown starts |
| Still below, cooldown not elapsed | No |
| Still below, cooldown elapsed | Yes, at most once per cooldown |
| Balance back above threshold | Yes, a recovery notice |
| 3 consecutive query failures | Yes, at most once per day |
| 3 consecutive meter-read failures | Yes, warning that the shown balance is stale |

Multiple recipients are comma-separated (full-width commas also work):

```
you@qq.com, roommate@example.com
```

> **The sender and the recipient are separate fields.** Listing an address as the sender does not send anything to it.

### SMTP authorization codes

QQ Mail requires an app-specific authorization code, **not your login password**. Enable *POP3/IMAP/SMTP* under QQ Mail settings → Account, then generate a code and paste it into `smtp.password`. Other providers work the same way; port 465 pairs with SSL, port 587 with STARTTLS.

---

## Command line

The web service already contains the scheduler, so these are mainly for debugging or headless use.

```bash
python monitor.py --check        # read cached balance only, no quota used
python monitor.py --refresh      # force one meter read, uses 1 quota unit
python monitor.py --test-email   # send a test email using config.json
python monitor.py --loop         # headless: same scheduler, no web console
python monitor.py                # single run, only acts when an interval is due
```

---

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | Current balance, history, quota, countdowns, scheduler state |
| `GET` | `/api/config` | Settings (the SMTP password is never returned, only `has_password`) |
| `POST` | `/api/config` | Partial update of any settings |
| `POST` | `/api/refresh` | Force a meter read |
| `POST` | `/api/test-email` | Send a test email with the current form values |
| `GET` | `/api/buildings?parent=<id>` | One level of the building tree |

---

## Project structure

```
.
├── server.py              Flask console + scheduler thread
├── monitor.py             Core logic: auth, queries, meter reads, alerts, mail
├── config.example.json    Configuration template
├── config.json            Your configuration (git-ignored)
├── start.bat              Windows one-click launcher
├── requirements.txt
└── web/
    ├── index.html
    ├── app.css
    └── app.js
```

`config.json`, `state.json`, `history.csv` and `monitor.log` are created locally and ignored by Git.

---

## Security notes

- `config.json` holds your SMTP authorization code. **Never commit it** — it is already in `.gitignore`; keep it that way
- The web console has **no authentication**. With `web.host` set to `0.0.0.0`, anyone who can reach the port can view your balance and change your settings. Keep the default `127.0.0.1` unless you actually need LAN access
- On campus Wi-Fi, client isolation often prevents device-to-device connections anyway
- Rotate your SMTP authorization code if it is ever exposed

---

## Limitations

- Bound to the BUPT Shahe campus portal; the building tree and endpoints are specific to it
- Depends on an undocumented API that the university could change at any time
- Balance history is only as fresh as the last meter read
- Running the console and `monitor.py --loop` at the same time is not recommended: both write `state.json`

---

## Disclaimer

Unofficial, unaffiliated project, intended for personal use. It reads only your own room's data through the portal's public query account using the same requests the website makes. Use at your own risk — a missed alert may mean a power cut.
