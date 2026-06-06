#!/usr/bin/env python3
import json
import os
import time
import urllib.request

WALLET = "0x7b0F7765e927a4eBB6E610d778d8e68f6e7F7aAc"
TENNIS_API_KEY = os.environ["TENNIS_API_KEY"]
NTFY_CHANNEL = "mfmoore-tennis"

STATE_FILE = "state.json"          # persisted via Actions cache
STALE_SECONDS = 10 * 60            # 10 minutes
STALE_THRESHOLD = 0.01             # 1% price movement


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def post(url, body, headers=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def is_suspended(status):
    s = (status or "").lower()
    return any(w in s for w in ["suspend", "delay", "rain", "interrupt"])


def fetch_tennis():
    matches = []
    for tour in ["atp", "wta"]:
        try:
            data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard")
            for event in data.get("events", []):
                comp = (event.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                status_type = event.get("status", {}).get("type", {})
                matches.append({
                    "id": f"espn-{event.get('id')}",
                    "status": status_type.get("name") or status_type.get("description") or "",
                    "players": [c.get("athlete", {}).get("displayName") or c.get("displayName") or "" for c in competitors],
                    "tour": tour.upper(),
                })
        except Exception as e:
            print(f"ESPN {tour} error: {e}")

    try:
        data = fetch(f"https://api.api-tennis.com/tennis/?method=get_livescore&APIkey={TENNIS_API_KEY}")
        for m in data.get("result", []):
            s = (m.get("event_status") or "").lower()
            if "suspend" in s or "delay" in s or "interrupt" in s or "rain" in s:
                status = "Suspended"
            elif "finish" in s or "ended" in s or s == "ft":
                status = "Final"
            elif not m.get("event_live") or m.get("event_live") == "0":
                status = "Scheduled"
            else:
                status = "In Progress"
            matches.append({
                "id": f"itf-{m.get('event_key')}",
                "status": status,
                "players": [m.get("event_first_player") or "", m.get("event_second_player") or ""],
                "tour": m.get("tournament_name") or "ITF",
            })
    except Exception as e:
        print(f"api-tennis error: {e}")

    return matches


def fetch_positions():
    try:
        data = fetch(f"https://data-api.polymarket.com/positions?user={WALLET}&sizeThreshold=0")
        positions = []
        for p in (data or []):
            size = float(p.get("size") or 0)
            current = float(p.get("currentValue") or 0)
            price = round(current / size, 4) if size > 0 else None
            positions.append({
                "title": p.get("title") or p.get("market") or "",
                "outcome": p.get("outcome") or "",
                "conditionId": p.get("conditionId") or "",
                "price": price,
            })
        return positions
    except Exception as e:
        print(f"Polymarket error: {e}")
        return []


def matches_position(pos, match):
    title = (pos["title"] or "").lower()
    outcome = (pos["outcome"] or "").lower()
    for player in match["players"]:
        last = player.split()[-1].lower() if player.split() else ""
        if len(last) > 2 and (last in title or last in outcome):
            return True
    return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted_suspensions": [], "price_history": {}, "stale_alerted": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(title, body, priority="high"):
    try:
        status = post(
            f"https://ntfy.sh/{NTFY_CHANNEL}",
            body,
            headers={"Title": title, "Priority": priority, "Tags": "tennis,warning"},
        )
        print(f"ntfy sent ({status}): {title}")
    except Exception as e:
        print(f"ntfy error: {e}")


def check_suspensions(matches, positions, state):
    alerted = set(state["alerted_suspensions"])
    suspended = [m for m in matches if is_suspended(m["status"])]
    print(f"Suspended matches: {len(suspended)}")

    for match in suspended:
        if match["id"] in alerted:
            continue
        if any(matches_position(p, match) for p in positions):
            players = " vs ".join(p for p in match["players"] if p)
            notify("Match Suspended ⚠", f"{players} ({match['tour']}) — check your Polymarket position")
            alerted.add(match["id"])

    state["alerted_suspensions"] = list(alerted)


def check_stale_prices(positions, state):
    now = time.time()
    history = state.get("price_history", {})
    stale_alerted = set(state.get("stale_alerted", []))

    for pos in positions:
        key = pos["conditionId"] or pos["title"]
        if not key or pos["price"] is None:
            continue

        price = pos["price"]
        record = history.get(key)

        if not record:
            # First time seeing this position
            history[key] = {"price": price, "last_moved": now, "last_checked": now}
            continue

        prev_price = record["price"]
        moved = abs(price - prev_price) >= STALE_THRESHOLD

        if moved:
            record["price"] = price
            record["last_moved"] = now
            # Clear stale alert so it can fire again if it stalls again later
            stale_alerted.discard(key)

        record["last_checked"] = now
        stale_since = now - record["last_moved"]

        if stale_since >= STALE_SECONDS and key not in stale_alerted:
            minutes = int(stale_since // 60)
            pct = round(price * 100, 1)
            notify(
                "Odds Frozen ⏸",
                f"{pos['title']} ({pos['outcome']}) — stuck at {pct}% for {minutes}m",
                priority="default",
            )
            stale_alerted.add(key)

    state["price_history"] = history
    state["stale_alerted"] = list(stale_alerted)


def main():
    matches = fetch_tennis()
    positions = fetch_positions()
    state = load_state()

    print(f"Live matches: {len(matches)}, Positions: {len(positions)}")

    check_suspensions(matches, positions, state)
    check_stale_prices(positions, state)

    save_state(state)


if __name__ == "__main__":
    main()
