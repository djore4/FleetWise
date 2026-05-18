#!/usr/bin/env python3
"""
build_vehicles.py — Fetches BEV/PHEV vehicles from Chargetrip API
and generates vehicles_pt.json for the FleetWise vehicle comparator.

SETUP (one-time):
  1. Log in to app.chargetrip.com
  2. Create a new App (or use an existing one)
  3. In the App settings, add "localhost" to Allowed Origins
  4. Copy the App ID (x-app-id) and paste below

RUN:
  pip install requests
  python3 build_vehicles.py
"""

import json
import sys
import requests

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
CLIENT_ID = "ff5d1cbb-89ec-4874-a8df-f30e7dbc0a4b"   # Workspace ID (x-client-id)
APP_ID    = "6a0ac0f2c4b109828252455e"                 # Application ID (x-app-id)
# ──────────────────────────────────────────────────────────────────────────────

ENDPOINT = "https://api.chargetrip.io/graphql"
HEADERS  = {
    "Content-Type": "application/json",
    "x-client-id":  CLIENT_ID,
    "x-app-id":     APP_ID,
}

PAGE_SIZE = 50

# Full vehicle fields — includes both basic and premium where available
VEHICLE_QUERY = """
query GetVehicles($page: Int, $size: Int) {
  vehicleList(page: $page, size: $size) {
    id
    naming {
      make
      model
      version
      chargetrip_version
    }
    drivetrain {
      type
    }
    connectors {
      standard
      power
      max_electric_power
    }
    battery {
      usable_kwh
      full_kwh
    }
    range {
      chargetrip_range {
        best
        worst
      }
      wltp
      wltp_range {
        best
        worst
      }
    }
    performance {
      acceleration
      top_speed
      electrical_range_min
      electrical_range_max
    }
    body {
      segment
      seats
      weight
      cargo_volume
      cargo_volume_max
    }
  }
}
"""

# Fallback query with fewer fields (if premium fields aren't available)
VEHICLE_QUERY_BASIC = """
query GetVehicles($page: Int, $size: Int) {
  vehicleList(page: $page, size: $size) {
    id
    naming {
      make
      model
      version
      chargetrip_version
    }
    drivetrain {
      type
    }
    connectors {
      standard
      power
    }
    battery {
      usable_kwh
    }
    range {
      chargetrip_range {
        best
        worst
      }
      wltp
    }
    performance {
      acceleration
      top_speed
    }
    body {
      segment
    }
  }
}
"""


def gql(query, variables=None):
    payload = {"query": query, "variables": variables or {}}
    r = requests.post(ENDPOINT, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
    return data["data"]


def fetch_all_vehicles():
    """Paginate through vehicleList until we have everything."""
    all_vehicles = []
    page = 0
    while True:
        print(f"  Fetching page {page} (size={PAGE_SIZE})…", end=" ", flush=True)
        try:
            data = gql(VEHICLE_QUERY, {"page": page, "size": PAGE_SIZE})
        except RuntimeError as e:
            if page == 0:
                print("\n  Full query failed, trying basic query…")
                data = gql(VEHICLE_QUERY_BASIC, {"page": page, "size": PAGE_SIZE})
            else:
                raise
        batch = data.get("vehicleList") or []
        print(f"{len(batch)} vehicles")
        if not batch:
            break
        all_vehicles.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return all_vehicles


# ── Segment translation ──────────────────────────────────────────────────────
SEGMENT_MAP = {
    "A": "city",         # city car
    "B": "hatchback",
    "C": "hatchback",
    "D": "sedan",
    "E": "sedan",        # executive
    "F": "sedan",        # luxury
    "S": "coupe",        # sports
    "M": "minivan",
    "J": "suv",          # off-road / crossover
    # lowercase variants
    "city": "city",
    "hatchback": "hatchback",
    "sedan": "sedan",
    "suv": "suv",
    "crossover": "suv",
    "coupe": "coupe",
    "minivan": "minivan",
    "pickup": "suv",
    "van": "minivan",
}

# ── Connector type classification ────────────────────────────────────────────
DC_STANDARDS = {
    "CCS1", "CCS2", "CHADEMO", "TESLA", "GB/T_DC",
    "IEC_62196_T3A", "IEC_62196_T3C",
}


def classify_connectors(connectors):
    """Return (max_ac_kw, max_dc_kw) from connector list."""
    if not connectors:
        return None, None
    ac_powers = []
    dc_powers = []
    for c in connectors:
        std = (c.get("standard") or "").upper()
        pwr = c.get("max_electric_power") or c.get("power") or 0
        if not pwr:
            continue
        if std in DC_STANDARDS or "CCS" in std or "CHAd" in std.lower() or "DC" in std:
            dc_powers.append(pwr)
        else:
            ac_powers.append(pwr)
    return (max(ac_powers) if ac_powers else None,
            max(dc_powers) if dc_powers else None)


def detect_type(vehicle):
    """Detect BEV vs PHEV from drivetrain type."""
    dt = vehicle.get("drivetrain") or {}
    dt_type = (dt.get("type") or "").upper()
    # Chargetrip uses: BEV, PHEV, HEV, ICE, etc.
    if "PHEV" in dt_type or "PLUGIN" in dt_type or "PLUG_IN" in dt_type:
        return "PHEV"
    if "BEV" in dt_type or "ELECTRIC" in dt_type or dt_type == "EV":
        return "BEV"
    # Fallback: if it has DC fast charging, likely BEV
    _, dc = classify_connectors(vehicle.get("connectors") or [])
    if dc and dc > 20:
        return "BEV"
    return None  # skip ICE/HEV


def safe_round(val, digits=1):
    if val is None:
        return None
    try:
        return round(float(val), digits)
    except (TypeError, ValueError):
        return None


def transform(v):
    """Map a Chargetrip vehicle to our vehicles_pt.json format."""
    naming = v.get("naming") or {}
    make   = (naming.get("make") or "").strip()
    model  = (naming.get("model") or "").strip()
    version_raw = (
        naming.get("chargetrip_version") or
        naming.get("version") or ""
    ).strip()

    if not make or not model:
        return None

    vtype = detect_type(v)
    if vtype not in ("BEV", "PHEV"):
        return None

    perf    = v.get("performance") or {}
    battery = v.get("battery") or {}
    body    = v.get("body") or {}
    rng     = v.get("range") or {}

    # Range: prefer chargetrip real-world average, fallback to WLTP
    ct_range = rng.get("chargetrip_range") or {}
    best     = ct_range.get("best")
    worst    = ct_range.get("worst")
    wltp     = rng.get("wltp")
    wltp_rng = rng.get("wltp_range") or {}

    if best and worst:
        range_km = safe_round((best + worst) / 2, 0)
    elif best:
        range_km = safe_round(best, 0)
    elif wltp:
        range_km = safe_round(wltp, 0)
    elif wltp_rng.get("best"):
        range_km = safe_round(wltp_rng["best"], 0)
    else:
        range_km = None

    ac_kw, dc_kw = classify_connectors(v.get("connectors") or [])

    # Power: Chargetrip gives kW, convert to cv (PS)
    power_kw = perf.get("power_kw") or perf.get("electrical_range_max")
    power_cv = round(power_kw * 1.36) if power_kw else None

    seg_raw = (body.get("segment") or "").strip()
    segment  = SEGMENT_MAP.get(seg_raw) or SEGMENT_MAP.get(seg_raw.lower()) or (seg_raw.lower() if seg_raw else None)

    cargo = body.get("cargo_volume") or body.get("cargo_volume_max")

    return {
        "make":       make,
        "model":      model,
        "version":    version_raw or f"{make} {model}",
        "type":       vtype,
        "power":      power_cv,
        "torque":     None,  # not in basic query; add vehiclePremium if needed
        "battery":    safe_round(battery.get("usable_kwh")),
        "chargeAC":   safe_round(ac_kw),
        "chargeDC":   safe_round(dc_kw),
        "range":      range_km,
        "accel":      safe_round(perf.get("acceleration")),
        "topSpeed":   safe_round(perf.get("top_speed"), 0),
        "segment":    segment,
        "cargo":      safe_round(cargo, 0),
        "drivetrain": None,  # Chargetrip drivetrain.type = BEV/PHEV, not FWD/RWD
    }


def main():
    print("── Chargetrip → vehicles_pt.json ──────────────────────")
    print("Fetching vehicles…")

    try:
        raw = fetch_all_vehicles()
    except requests.exceptions.ConnectionError as e:
        print(f"\n❌  Network error: {e}")
        print("   Check your internet connection.")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"\n❌  HTTP error: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n❌  GraphQL error: {e}")
        print("\n   Common causes:")
        print("   • APP_ID is wrong — check app.chargetrip.com → your app → App ID")
        print("   • Your origin/localhost is not in the app's Allowed Origins")
        sys.exit(1)

    print(f"\nTotal raw vehicles from API: {len(raw)}")

    vehicles = []
    skipped  = 0
    for v in raw:
        rec = transform(v)
        if rec:
            vehicles.append(rec)
        else:
            skipped += 1

    bev_count  = sum(1 for v in vehicles if v["type"] == "BEV")
    phev_count = sum(1 for v in vehicles if v["type"] == "PHEV")

    print(f"Kept: {len(vehicles)} ({bev_count} BEV, {phev_count} PHEV)")
    print(f"Skipped (ICE/HEV/incomplete): {skipped}")

    out_path = "vehicles_pt.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(vehicles, f, ensure_ascii=False, indent=2)

    print(f"\n✅  Saved {len(vehicles)} vehicles → {out_path}")
    print("\nSample (first 3):")
    for v in vehicles[:3]:
        print(f"  {v['make']} {v['model']} — {v['type']} — {v['range']} km")


if __name__ == "__main__":
    main()
