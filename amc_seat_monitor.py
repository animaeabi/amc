#!/usr/bin/env python3
"""AMC seat availability radar with Telegram alerts.

Watches one theater + one movie over a rolling date window and alerts the
moment seats (optionally: specific target seats) become available — including
when brand-new showtimes drop.

Modes:
  --setup   verify AMC key + Telegram, resolve theatre, capture chat id, send test msg
  --map     dump live seat maps (seatmap.txt + seatmaps.json) for matching showtimes
  --once    single availability check
  (none)    monitor loop

Config: config.json (non-secret). Secrets via env: AMC_VENDOR_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (chat id optional — auto-captured from
the bot's incoming messages and persisted to state.json).
No third-party packages required (stdlib only).
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

AMC_BASE = "https://api.amctheatres.com"
TELEGRAM_BASE = "https://api.telegram.org"
ACCESSIBLE_TYPES = {"Wheelchair", "Companion"}
HERE = os.path.dirname(os.path.abspath(__file__))


def http_json(url, headers=None, payload=None, timeout=25):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RateLimited(Exception):
    pass


class Amc:
    def __init__(self, key):
        if not key:
            raise SystemExit("AMC_VENDOR_KEY missing (env or config amc_vendor_key)")
        self.h = {"X-AMC-Vendor-Key": key, "Accept": "application/json"}
        self.showtime_style = None

    def get(self, path, params=None):
        url = AMC_BASE + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        try:
            return http_json(url, headers=self.h)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                raise RateLimited(f"HTTP {e.code} on {path}")
            raise

    def find_theatres(self, q):
        d = self.get("/v2/theatres", {"name": q, "pageSize": 20})
        return (d.get("_embedded") or {}).get("theatres", [])

    def theatre(self, tid):
        return self.get(f"/v2/theatres/{tid}")

    def showtimes(self, tid, date):
        styles = [
            ("path-mmddyyyy", f"/v2/theatres/{tid}/showtimes/{date.strftime('%m-%d-%Y')}"),
            ("path-iso", f"/v2/theatres/{tid}/showtimes/{date.isoformat()}"),
            ("query-iso", f"/v2/theatres/{tid}/showtimes?date={date.isoformat()}"),
        ]
        if self.showtime_style:
            styles.sort(key=lambda s: s[0] != self.showtime_style)
        err = None
        for style, path in styles:
            try:
                out, page = [], 1
                while True:
                    sep = "&" if "?" in path else "?"
                    d = self.get(f"{path}{sep}pageSize=100&pageNumber={page}")
                    batch = (d.get("_embedded") or {}).get("showtimes", [])
                    out.extend(batch)
                    if not batch or len(out) >= d.get("count", len(out)):
                        self.showtime_style = style
                        return out
                    page += 1
            except RateLimited:
                raise
            except Exception as e:
                err = e
        raise RuntimeError(f"showtimes fetch failed for {date}: {err}")

    def seating(self, tid, performance):
        return self.get(f"/v2/seating-layouts/{tid}/{performance}")


# ---------- config / state ----------

def load_cfg(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["amc_vendor_key"] = os.environ.get("AMC_VENDOR_KEY") or cfg.get("amc_vendor_key", "")
    cfg["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token", "")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
    return cfg


def load_state():
    p = os.path.join(HERE, "state.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_state(st):
    with open(os.path.join(HERE, "state.json"), "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def now_local(cfg):
    tz = cfg.get("timezone", "America/Los_Angeles")
    if ZoneInfo:
        return dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    return dt.datetime.now()


# ---------- seat spec ----------

def normalize_seat(name):
    return re.sub(r"\s+", "", str(name or "")).upper()


def parse_seat_spec(spec):
    spec = (spec or "").strip()
    if not spec:
        return lambda name: True
    rules = []
    for tok in [t.strip().upper().replace(" ", "") for t in spec.split(",") if t.strip()]:
        m = re.fullmatch(r"([A-Z]+)(\d+)-(?:([A-Z]+))?(\d+)", tok)
        if m and (m.group(3) is None or m.group(3) == m.group(1)):
            rules.append((m.group(1), int(m.group(2)), int(m.group(4))))
            continue
        m = re.fullmatch(r"([A-Z]+)(\d+)", tok)
        if m:
            rules.append((m.group(1), int(m.group(2)), int(m.group(2))))
            continue
        m = re.fullmatch(r"([A-Z]+)", tok)
        if m:
            rules.append((m.group(1), None, None))
            continue
        raise ValueError(f"can't parse seat token: {tok!r} (use F10, F8-F14, or F)")

    def match(name):
        m = re.fullmatch(r"([A-Z]+)(\d+)", normalize_seat(name))
        if not m:
            return False
        row, num = m.group(1), int(m.group(2))
        return any(row == r and (lo is None or lo <= num <= hi) for r, lo, hi in rules)

    return match


def available_seats(layout, include_accessible=False):
    out = []
    for s in layout.get("seats") or []:
        if s.get("type") == "NotASeat":
            continue
        if not include_accessible and s.get("type") in ACCESSIBLE_TYPES:
            continue
        if s.get("available") is True:
            out.append(normalize_seat(s.get("seatName")) or f"R{s.get('row')}C{s.get('column')}")
    return sorted(out)


# ---------- telegram ----------

def tg(token, method, payload=None):
    return http_json(f"{TELEGRAM_BASE}/bot{token}/{method}", payload=payload)


def tg_send(cfg, text):
    return tg(cfg["telegram_bot_token"], "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })


def capture_chat_id(cfg, st, log):
    if cfg.get("telegram_chat_id"):
        return True
    if st.get("telegram_chat_id"):
        cfg["telegram_chat_id"] = st["telegram_chat_id"]
        return True
    try:
        upd = tg(cfg["telegram_bot_token"], "getUpdates")
        msgs = [u.get("message") or u.get("edited_message") for u in upd.get("result", [])]
        msgs = [m for m in msgs if m]
        if msgs:
            chat = msgs[-1]["chat"]
            cfg["telegram_chat_id"] = chat["id"]
            st["telegram_chat_id"] = chat["id"]
            st["telegram_chat_name"] = chat.get("username") or chat.get("first_name") or ""
            save_state(st)
            log(f"captured telegram chat id ({st['telegram_chat_name']})")
            return True
    except Exception as e:
        log(f"! chat id capture failed: {e}")
    log("no telegram chat id yet — open your bot in Telegram and send it any message")
    return False


# ---------- core ----------

def resolve_theatre(amc, cfg, st, log):
    if not cfg.get("theatre_id"):
        if st.get("theatre_id"):
            cfg["theatre_id"], cfg["theatre_name"] = st["theatre_id"], st.get("theatre_name")
            return
        ts = amc.find_theatres(cfg.get("theatre_query", ""))
        if not ts:
            raise SystemExit(f"no theatre matches {cfg.get('theatre_query')!r}")
        t = ts[0]
        if len(ts) > 1:
            log(f"note: {len(ts)} theatres matched; using first: {t.get('longName')}")
            for x in ts[:8]:
                loc = x.get("location") or {}
                log(f"   candidate id={x['id']} {x.get('longName')} ({loc.get('city')}, {loc.get('state')})")
        cfg["theatre_id"] = t["id"]
        cfg["theatre_name"] = t.get("longName") or t.get("name")
        st["theatre_id"], st["theatre_name"] = cfg["theatre_id"], cfg["theatre_name"]
        save_state(st)
    if not cfg.get("theatre_name"):
        try:
            t = amc.theatre(cfg["theatre_id"])
            cfg["theatre_name"] = t.get("longName") or t.get("name")
        except Exception:
            cfg["theatre_name"] = f"theatre {cfg['theatre_id']}"


def wanted_attributes_ok(showtime, cfg):
    want = [w.lower() for w in cfg.get("require_attributes", []) if w]
    if not want:
        return True
    codes = " ".join(
        f"{a.get('code','')} {a.get('name','')}".lower()
        for a in (showtime.get("attributes") or [])
    )
    return all(w in codes for w in want)


def matching_showtimes(amc, cfg, log):
    now = now_local(cfg)
    start = now.date()
    if cfg.get("date_start"):
        start = max(start, dt.date.fromisoformat(cfg["date_start"]))
    end = start + dt.timedelta(days=int(cfg.get("rolling_days", 14)))
    if cfg.get("date_end"):
        end = min(end, dt.date.fromisoformat(cfg["date_end"]))
    found = []
    d = start
    while d <= end:
        try:
            shows = amc.showtimes(cfg["theatre_id"], d)
        except RateLimited:
            raise
        except Exception as e:
            log(f"  ! showtimes {d}: {e}")
            d += dt.timedelta(days=1)
            continue
        for s in shows:
            if cfg["movie_query"].lower() not in (s.get("movieName") or "").lower():
                continue
            if not wanted_attributes_ok(s, cfg):
                continue
            try:
                when = dt.datetime.fromisoformat(s.get("showDateTimeLocal", ""))
                if when < now - dt.timedelta(minutes=15):
                    continue
            except (ValueError, TypeError):
                pass
            found.append(s)
        d += dt.timedelta(days=1)
    return found


def check(amc, cfg, st, log):
    """-> (hits, new_drop) ; hit = {showtime, target, total, is_new}"""
    match = parse_seat_spec(cfg.get("target_seats", ""))
    seen = set(st.get("seen_showtimes", []))
    shows = matching_showtimes(amc, cfg, log)
    log(f"  {len(shows)} matching future showtimes")
    hits, new_drop = [], False
    for s in shows:
        sid = str(s["id"])
        is_new = bool(seen) and sid not in seen
        try:
            layout = amc.seating(cfg["theatre_id"], s["id"])
        except RateLimited:
            raise
        except Exception as e:
            log(f"    ! seating {sid}: {e}")
            continue
        opens = available_seats(layout, cfg.get("include_accessible", False))
        target = [n for n in opens if match(n)]
        already = set(st.get("alerted", {}).get(sid, []))
        fresh = [n for n in target if n not in already]
        if is_new:
            new_drop = True
        if len(target) >= int(cfg.get("min_seats", 1)) and fresh:
            hits.append({"showtime": s, "target": target, "total": len(opens), "is_new": is_new})
        time.sleep(float(cfg.get("request_gap", 0.1)))
    st["seen_showtimes"] = sorted(seen | {str(s["id"]) for s in shows})
    save_state(st)
    return hits, new_drop


def fmt_when(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    except (ValueError, TypeError):
        return str(iso)


def alert_text(cfg, hits, new_drop):
    head = "🚨 <b>NEW SHOWTIMES DROPPED</b> — " if new_drop else "🎬 <b>SEATS OPEN</b> — "
    lines = [head + str(cfg["movie_query"]).title(),
             f"📍 {cfg.get('theatre_name', 'AMC')}"]
    for h in hits[:10]:
        s = h["showtime"]
        seats = ", ".join(h["target"][:10]) + ("…" if len(h["target"]) > 10 else "")
        tag = " 🆕" if h["is_new"] else ""
        lines.append(f"\n<b>{fmt_when(s.get('showDateTimeLocal'))}</b>{tag}")
        lines.append(f"{len(h['target'])} in your range: {seats}" if cfg.get("target_seats")
                     else f"{h['total']} seats open: {seats}")
        lines.append(f"https://www.amctheatres.com/showtimes/{s.get('id')}/seats")
    if len(hits) > 10:
        lines.append(f"\n…and {len(hits) - 10} more showtimes")
    lines.append("\n⚡ Go go go — bots are racing you.")
    return "\n".join(lines)


def record_alerts(st, hits):
    alerted = st.setdefault("alerted", {})
    for h in hits:
        sid = str(h["showtime"]["id"])
        alerted[sid] = sorted(set(alerted.get(sid, [])) | set(h["target"]))
    save_state(st)


# ---------- modes ----------

def do_setup(amc, cfg, st, log):
    print("1) AMC key + theatre…")
    resolve_theatre(amc, cfg, st, log)
    print(f"   OK — {cfg['theatre_name']} (id {cfg['theatre_id']})")
    shows = matching_showtimes(amc, cfg, log)
    print(f"   {len(shows)} future showtimes match {cfg['movie_query']!r} in window")
    for s in shows[:20]:
        attrs = ",".join(a.get("code", "") for a in (s.get("attributes") or [])[:6])
        print(f"     {fmt_when(s.get('showDateTimeLocal'))}  soldOut={s.get('isSoldOut')}  [{attrs}]")
    print("2) Telegram bot…")
    me = tg(cfg["telegram_bot_token"], "getMe")
    bot = me["result"]["username"]
    print(f"   OK — @{bot}")
    got_chat = capture_chat_id(cfg, st, log)
    report = {
        "theatre_id": cfg["theatre_id"], "theatre_name": cfg["theatre_name"],
        "bot_username": bot, "chat_id_captured": bool(got_chat),
        "matching_showtimes": [
            {"id": s.get("id"), "when": s.get("showDateTimeLocal"),
             "movie": s.get("movieName"), "soldOut": s.get("isSoldOut"),
             "attrs": [a.get("code") for a in (s.get("attributes") or [])]}
            for s in shows],
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
    }
    with open(os.path.join(HERE, "setup_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    if got_chat:
        tg_send(cfg, f"✅ Seat radar connected.\nWatching <b>{str(cfg['movie_query']).title()}</b> at "
                     f"<b>{cfg['theatre_name']}</b> (rolling {cfg.get('rolling_days', 14)}-day window).\n"
                     f"{len(shows)} showtimes currently listed.")
        print("3) Test message sent — check Telegram.")
        return 0
    print("3) SKIPPED test message — message your bot @" + bot + " first, then rerun setup.")
    return 0  # setup still useful without chat id


def do_map(amc, cfg, st, log):
    resolve_theatre(amc, cfg, st, log)
    shows = matching_showtimes(amc, cfg, log)
    print(f"{len(shows)} matching showtimes")
    dump, lines = [], []
    for s in shows:
        title = f"=== {s.get('movieName')} — {fmt_when(s.get('showDateTimeLocal'))} (id {s['id']}) ==="
        print("\n" + title)
        lines.append("\n" + title)
        try:
            layout = amc.seating(cfg["theatre_id"], s["id"])
        except Exception as e:
            print(f"  seating fetch failed: {e}")
            lines.append(f"  seating fetch failed: {e}")
            continue
        dump.append({
            "id": s.get("id"), "movie": s.get("movieName"),
            "when": s.get("showDateTimeLocal"), "soldOut": s.get("isSoldOut"),
            "attrs": [a.get("code") for a in (s.get("attributes") or [])],
            "rows": layout.get("rows"), "columns": layout.get("columns"),
            "seats": [{"n": normalize_seat(x.get("seatName")), "r": x.get("row"),
                       "c": x.get("column"), "t": x.get("type"),
                       "tier": x.get("seatTier"), "a": bool(x.get("available"))}
                      for x in layout.get("seats") or []],
        })
        grid = {}
        for seat in layout.get("seats") or []:
            grid.setdefault(seat.get("row"), []).append(seat)
        for r in sorted(k for k in grid if k is not None):
            cells, label = [], "?"
            for seat in sorted(grid[r], key=lambda x: x.get("column") or 0):
                nm = normalize_seat(seat.get("seatName"))
                if seat.get("type") == "NotASeat":
                    cells.append("     ")
                    continue
                m = re.fullmatch(r"([A-Z]+)\d+", nm)
                if m:
                    label = m.group(1)
                mark = "+" if seat.get("available") else "x"
                if seat.get("type") in ACCESSIBLE_TYPES:
                    mark = "w" if seat.get("available") else "x"
                cells.append(f"{nm:>4}{mark}")
            row_line = f"  {label:>2}| " + " ".join(cells)
            print(row_line)
            lines.append(row_line)
        opens = available_seats(layout, cfg.get("include_accessible", False))
        foot = f"  OPEN ({len(opens)}): {', '.join(opens) if opens else '(none)'}"
        print(foot)
        lines.append(foot)
        time.sleep(float(cfg.get("request_gap", 0.1)))
    with open(os.path.join(HERE, "seatmaps.json"), "w") as f:
        json.dump({"theatre": cfg.get("theatre_name"), "generated_at":
                   dt.datetime.utcnow().isoformat() + "Z", "showtimes": dump}, f)
    with open(os.path.join(HERE, "seatmap.txt"), "w") as f:
        f.write("legend: + open, x taken, w wheelchair/companion open\n")
        f.write("\n".join(lines))
    print("\nwrote seatmaps.json + seatmap.txt")
    return 0


def monitor(amc, cfg, st, args, log):
    resolve_theatre(amc, cfg, st, log)
    capture_chat_id(cfg, st, log)
    deadline = time.time() + args.duration if args.duration else None
    log(f"radar on: {cfg['movie_query']!r} @ {cfg['theatre_name']} | poll {args.poll}s | "
        f"target={cfg.get('target_seats') or 'any seat'} | min_seats={cfg.get('min_seats', 1)}")
    while True:
        try:
            hits, new_drop = check(amc, cfg, st, log)
        except RateLimited as e:
            log(f"! rate limited ({e}) — backing off 60s")
            hits, new_drop = [], False
            time.sleep(60)
        except Exception as e:
            log(f"! check failed: {e}")
            hits, new_drop = [], False
        if hits:
            if not cfg.get("telegram_chat_id"):
                capture_chat_id(cfg, st, log)
            log(f"SEATS FOUND in {len(hits)} showtime(s){' (NEW DROP)' if new_drop else ''}")
            if args.dry_run or not cfg.get("telegram_chat_id"):
                print(alert_text(cfg, hits, new_drop))
                print("WOULD_ALERT")
            else:
                try:
                    tg_send(cfg, alert_text(cfg, hits, new_drop))
                    log("telegram alert sent")
                    print("ALERT_SENT", flush=True)
                except Exception as e:
                    log(f"! telegram send failed: {e}")
            record_alerts(st, hits)
        if args.once:
            return 0 if hits else 1
        if deadline and time.time() + args.poll > deadline:
            log("run window over")
            return 0
        time.sleep(args.poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--poll", type=int, default=90)
    ap.add_argument("--duration", type=int, default=0, help="seconds; 0 = forever")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    st = load_state()
    amc = Amc(cfg["amc_vendor_key"])

    def log(m):
        print(f"[{dt.datetime.now().strftime('%m-%d %H:%M:%S')}] {m}", flush=True)

    if args.setup:
        return do_setup(amc, cfg, st, log)
    if args.map:
        return do_map(amc, cfg, st, log)
    return monitor(amc, cfg, st, args, log)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)

