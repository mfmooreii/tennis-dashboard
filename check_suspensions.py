#!/usr/bin/env python3
import json
import os
import urllib.request

WALLET = "0x7b0F7765e927a4eBB6E610d778d8e68f6e7F7aAc"
TENNIS_API_KEY = os.environ["TENNIS_API_KEY"]
NTFY_CHANNEL = "mfmoore-tennis"
ALERTED_FILE = "/tmp/alerted_ids.json"


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
        return [{"title": p.get("title") or p.get("market") or "", "outcome": p.get("outcome") or ""} for p in (data or [])]
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


def load_alerted():
    try:
        with open(ALERTED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_alerted(ids):
    with open(ALERTED_FILE, "w") as f:
        json.dump(list(ids), f)


def notify(title, body):
    try:
        status = post(
            f"https://ntfy.sh/{NTFY_CHANNEL}",
            body,
            headers={"Title": title, "Priority": "high", "Tags": "tennis,warning"},
        )
        print(f"ntfy sent: {status} — {title}")
    except Exception as e:
        print(f"ntfy error: {e}")


def main():
    matches = fetch_tennis()
    positions = fetch_positions()
    alerted = load_alerted()

    suspended = [m for m in matches if is_suspended(m["status"])]
    print(f"Live matches: {len(matches)}, Suspended: {len(suspended)}, Positions: {len(positions)}")

    for match in suspended:
        if match["id"] in alerted:
            continue
        linked = any(matches_position(p, match) for p in positions)
        if linked:
            players = " vs ".join(p for p in match["players"] if p)
            notify("Match Suspended ⚠", f"{players} ({match['tour']}) — check your Polymarket position")
            alerted.add(match["id"])

    save_alerted(alerted)


if __name__ == "__main__":
    main()
