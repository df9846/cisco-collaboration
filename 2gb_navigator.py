#!/usr/bin/env python3
"""
Scan Webex Control Hub devices and identify connected peripherals that are
Cisco Room Navigators with DRAM == 2 via cloud xAPI.

Verification added:
- Must have ConnectedDevice[*].Name == 'Cisco Room Navigator' (case-insensitive exact match)
- Must have ConnectedDevice[*].DRAM == 2
- Output shows the *Navigator serial number* (peripheral SN), not the codec SN.

Uses (as confirmed by you):
GET https://webexapis.com/v1/xapi/status?name=Peripherals.ConnectedDevice[*].DRAM&deviceId={DeviceID}

This script queries DRAM + SerialNumber + Name in one request:
  name=Peripherals.ConnectedDevice[*].DRAM,Peripherals.ConnectedDevice[*].SerialNumber,Peripherals.ConnectedDevice[*].Name

Console output:
- Navigator Serial Number
- DRAM (raw)
- ConnectedDevice Name
- Codec/endpoint display name it is connected to

Notes:
- Some devices will fail (offline/unsupported/no xAPI access). We keep going.
- Tune MAX_WORKERS to balance speed vs 429 throttling.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


# ==========================================================
# 🔐 PASTE YOUR TOKEN HERE
# ==========================================================
WEBEX_TOKEN = "{your_webex_token}"
# ==========================================================

# ----------------------------
# Performance knobs
# ----------------------------
MAX_WORKERS = 12          # increase slowly; too high -> 429 throttling
REQUEST_TIMEOUT = 25
MAX_RETRIES = 8

# ----------------------------
# Logging knobs
# ----------------------------
DEBUG = False             # True = verbose per-device troubleshooting
LOG_EVERY_N = 200         # progress log cadence for large orgs

# ----------------------------
# Match criteria
# ----------------------------
TARGET_DRAM_VALUE = 2
TARGET_DEVICE_NAME = "Cisco Room Navigator"  # exact value (we match case-insensitively)


WEBEX_API_BASE = "https://webexapis.com/v1"


# ----------------------------
# Logging setup
# ----------------------------
logger = logging.getLogger("navigator_2gb_scan")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


def bearer_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def request_with_backoff(
    session: requests.Session,
    method: str,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> requests.Response:
    """
    Retries throttling (429) and transient 5xx errors with exponential-ish backoff.
    """
    last_resp: Optional[requests.Response] = None
    for attempt in range(max_retries):
        resp = session.request(method, url, headers=headers, params=params, timeout=timeout)
        last_resp = resp

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            sleep_s = float(retry_after) if retry_after else min(2 ** attempt, 30)
            logger.warning(f"429 throttled. Sleeping {sleep_s:.1f}s then retrying...")
            time.sleep(sleep_s)
            continue

        if resp.status_code >= 500:
            sleep_s = min(2 ** attempt, 30)
            logger.warning(f"{resp.status_code} server error. Sleeping {sleep_s:.1f}s then retrying...")
            time.sleep(sleep_s)
            continue

        return resp

    if last_resp is None:
        raise RuntimeError("No HTTP response received (unexpected).")
    return last_resp


def iter_paginated(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    params: Dict[str, Any],
) -> Iterable[Dict[str, Any]]:
    """
    Webex pagination via Link: <...>; rel="next"
    """
    next_url = url
    next_params = dict(params)

    while next_url:
        resp = request_with_backoff(session, "GET", next_url, headers=headers, params=next_params)
        if not resp.ok:
            raise RuntimeError(f"GET {next_url} failed: {resp.status_code} {resp.text}")

        data = resp.json()
        for it in data.get("items", []):
            yield it

        link = resp.headers.get("Link", "")
        next_link = None
        if link:
            parts = [p.strip() for p in link.split(",")]
            for p in parts:
                if 'rel="next"' in p:
                    start = p.find("<")
                    end = p.find(">")
                    if start != -1 and end != -1 and end > start:
                        next_link = p[start + 1 : end]
                        break

        if next_link:
            next_url = next_link
            next_params = {}  # next link already includes query params
        else:
            next_url = None


def normalize_connected_device_list(connected: Any) -> List[Dict[str, Any]]:
    """
    ConnectedDevice may be:
      - list: [ {...}, {...} ]
      - dict: { "0": {...}, "1": {...} }
    Normalize into list[dict].
    """
    if connected is None:
        return []

    if isinstance(connected, list):
        return [x for x in connected if isinstance(x, dict)]

    if isinstance(connected, dict):
        out: List[Dict[str, Any]] = []
        keys = list(connected.keys())
        keys.sort(key=lambda k: int(k) if str(k).isdigit() else 10_000_000)
        for k in keys:
            v = connected.get(k)
            if isinstance(v, dict):
                out.append(v)
        return out

    return []


def xapi_get_connected_device_info(
    session: requests.Session,
    headers: Dict[str, str],
    device_id: str,
) -> List[Dict[str, Any]]:
    """
    Calls the confirmed working endpoint and returns ConnectedDevice entries, each potentially including:
      - DRAM
      - SerialNumber
      - Name
      - id
      - etc.
    """
    url = f"{WEBEX_API_BASE}/xapi/status"
    params = {
        "name": (
            "Peripherals.ConnectedDevice[*].DRAM,"
            "Peripherals.ConnectedDevice[*].SerialNumber,"
            "Peripherals.ConnectedDevice[*].Name"
        ),
        "deviceId": device_id,
    }

    resp = request_with_backoff(session, "GET", url, headers=headers, params=params)
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.text}")

    payload = resp.json()
    result = payload.get("result", {})
    peripherals = result.get("Peripherals", {})
    connected = peripherals.get("ConnectedDevice", [])

    return normalize_connected_device_list(connected)


def dram_is_target(v: Any, target: int = TARGET_DRAM_VALUE) -> bool:
    """
    Your tenant reports DRAM as small ints (e.g., 2 or 4). Match directly, tolerate strings.
    """
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return int(v) == target
    if isinstance(v, str):
        s = v.strip()
        return s.isdigit() and int(s) == target
    return False


def name_is_room_navigator(v: Any, target_name: str = TARGET_DEVICE_NAME) -> bool:
    """
    Must match exactly 'Cisco Room Navigator' per your requirement, case-insensitively.
    """
    if v is None:
        return False
    s = str(v).strip()
    return s.casefold() == target_name.strip().casefold()


def scan_one_device(device: Dict[str, Any], token: str) -> Tuple[List[Tuple[str, Any, str, str]], str]:
    """
    Returns:
      (matches, error)

    matches is list of tuples:
      (navigator_serial, dram_raw, connected_device_name, codec_display_name)
    """
    device_id = device.get("id")
    codec_name = device.get("displayName") or device.get("name") or "Unknown"

    if not device_id:
        return ([], "missing device id")

    session = requests.Session()
    headers = bearer_headers(token)

    try:
        connected = xapi_get_connected_device_info(session, headers, device_id)

        if DEBUG and connected:
            logger.debug(f"{codec_name}: sample peripherals -> {connected[:4]}")

        matches: List[Tuple[str, Any, str, str]] = []
        for per in connected:
            dram = per.get("DRAM")
            per_name = per.get("Name")
            nav_serial = per.get("SerialNumber") or per.get("Serial")  # fallback just in case

            # Verification: must be Cisco Room Navigator AND DRAM==2
            if name_is_room_navigator(per_name) and dram_is_target(dram, TARGET_DRAM_VALUE):
                if not nav_serial:
                    nav_serial = "(SerialNumber not returned)"
                matches.append((str(nav_serial), dram, str(per_name), codec_name))

        return (matches, "")

    except Exception as e:
        return ([], str(e))


def print_results(matches: List[Tuple[str, Any, str, str]]) -> None:
    """
    matches: (navigator_serial, dram_raw, connected_device_name, codec_display_name)
    """
    print("\n" + "=" * 88)
    print(f" 2GB Cisco Room Navigators (Name verified == '{TARGET_DEVICE_NAME}')")
    print("=" * 88)

    if not matches:
        print("No matches found.\n")
        return

    # Deduplicate by navigator serial + dram + name (avoid duplicates if any)
    seen = set()
    deduped: List[Tuple[str, Any, str, str]] = []
    for nav_sn, dram, per_name, codec in matches:
        key = (nav_sn, str(dram), per_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append((nav_sn, dram, per_name, codec))

    for i, (nav_sn, dram, per_name, codec) in enumerate(deduped, start=1):
        print(f"{i:>3}. 📟 Navigator Serial: {nav_sn}")
        print(f"     🏷️  Name: {per_name}")
        print(f"     🧠 DRAM: {dram}")
        print(f"     🎥 Connected to: {codec}")
        print()

    print(f"Total 2GB Cisco Room Navigators found: {len(deduped)}\n")


def main() -> int:
    if not WEBEX_TOKEN or "PASTE_YOUR_TOKEN_HERE" in WEBEX_TOKEN:
        print("❌ Paste your token into WEBEX_TOKEN at the top of the script.")
        return 2

    inv_session = requests.Session()
    headers = bearer_headers(WEBEX_TOKEN)

    logger.info("Listing devices from /v1/devices ...")
    devices = list(iter_paginated(inv_session, f"{WEBEX_API_BASE}/devices", headers, {"max": 500}))
    logger.info(f"Devices retrieved: {len(devices)}")

    logger.info(
        "Querying xAPI status for ConnectedDevice DRAM+SerialNumber+Name "
        f"with up to {MAX_WORKERS} workers ..."
    )

    all_matches: List[Tuple[str, Any, str, str]] = []
    errors: List[str] = []

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(scan_one_device, d, WEBEX_TOKEN) for d in devices]

        for fut in as_completed(futures):
            completed += 1
            matches, err = fut.result()

            if err:
                if DEBUG:
                    logger.debug(f"FAIL: {err}")
                errors.append(err)
            else:
                for nav_sn, dram, per_name, codec in matches:
                    all_matches.append((nav_sn, dram, per_name, codec))
                    logger.info(f"MATCH: NavigatorSN={nav_sn} DRAM={dram} Name='{per_name}' (connected to {codec})")

            if completed % LOG_EVERY_N == 0:
                logger.info(f"Progress: {completed}/{len(devices)} scanned | matches={len(all_matches)}")

    print_results(all_matches)

    if errors:
        print(f"Note: {len(errors)} devices could not be queried (offline/unsupported/no xAPI access/etc).")
        if DEBUG:
            print("First 20 failures:")
            for i, e in enumerate(errors[:20], start=1):
                print(f"{i:>2}. {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
