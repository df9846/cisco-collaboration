import re
import sys
import time
import argparse
import logging
from datetime import datetime

import requests


# ============================================================
# Paste your bearer tokens here
# ============================================================

WEBEX_TOKEN = "YOUR_WEBEX_ADMIN_TOKEN"
THOUSANDEYES_TOKEN = "YOUR_THOUSANDEYES_ADMIN_TOKEN"

# ============================================================


WEBEX_BASE_URL = "https://webexapis.com/v1"

TE_BASE_URL = "https://api.thousandeyes.com/v7"
TE_ENDPOINT_AGENTS_URL = f"{TE_BASE_URL}/endpoint/agents"

# All Endpoint Agents are assumed to use Advantage license.
TE_LICENSE_TYPE = "advantage"

# Only ThousandEyes Endpoint Agents named exactly SEP + 12 hexadecimal characters are considered.
# Example: SEPAABBCCDDEEFF
SEP_MAC_PATTERN = re.compile(r"^SEP([0-9A-Fa-f]{12})$")


def setup_logging(log_file):
    logger = logging.getLogger("webex_te_endpoint_rename")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def validate_tokens():
    if not WEBEX_TOKEN or WEBEX_TOKEN == "PASTE_YOUR_WEBEX_CONTROL_HUB_ACCESS_TOKEN_HERE":
        raise RuntimeError("Please paste your Webex Control Hub access token into WEBEX_TOKEN.")

    if not THOUSANDEYES_TOKEN or THOUSANDEYES_TOKEN == "PASTE_YOUR_THOUSANDEYES_ACCESS_TOKEN_HERE":
        raise RuntimeError("Please paste your ThousandEyes access token into THOUSANDEYES_TOKEN.")


def normalize_mac(mac):
    """
    Converts MAC formats like:
      AA:BB:CC:DD:EE:FF
      AA-BB-CC-DD-EE-FF
      AABB.CCDD.EEFF
      AABBCCDDEEFF

    Into:
      AABBCCDDEEFF
    """
    if not mac:
        return None

    cleaned = (
        str(mac)
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace(" ", "")
        .upper()
    )

    if re.fullmatch(r"[0-9A-F]{12}", cleaned):
        return cleaned

    return None


def parse_next_link_header(link_header):
    """
    Parses RFC5988-style Link headers and returns the URL for rel="next", if present.
    """
    if not link_header:
        return None

    parts = link_header.split(",")

    for part in parts:
        sections = part.split(";")
        if len(sections) < 2:
            continue

        url_part = sections[0].strip()
        rel_parts = [s.strip() for s in sections[1:]]

        if any('rel="next"' in rel for rel in rel_parts):
            return url_part.strip("<>")

    return None


def get_next_url_from_json(data):
    """
    Attempts to extract a next-page URL from common JSON/HAL pagination formats.
    """
    if not isinstance(data, dict):
        return None

    links = data.get("_links") or data.get("links") or {}

    if isinstance(links, dict):
        next_link = links.get("next")

        if isinstance(next_link, str):
            return next_link

        if isinstance(next_link, dict):
            return next_link.get("href")

    pagination = data.get("pagination") or data.get("pages") or {}

    if isinstance(pagination, dict):
        next_url = (
            pagination.get("next")
            or pagination.get("nextUrl")
            or pagination.get("next_url")
        )
        if next_url:
            return next_url

    return None


def get_next_url(response, data):
    """
    Looks for next-page URL in both the HTTP Link header and JSON body.
    """
    next_url = parse_next_link_header(response.headers.get("Link"))

    if next_url:
        return next_url

    return get_next_url_from_json(data)


def request_with_retries(method, url, headers, logger, **kwargs):
    """
    Basic retry handler for rate limits and transient server errors.
    """
    max_attempts = 5
    backoff_seconds = 2

    response = None

    for attempt in range(1, max_attempts + 1):
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=60,
            **kwargs
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_time = int(retry_after) if retry_after and retry_after.isdigit() else backoff_seconds

            logger.warning(
                f"Rate limited on {method} {url}. "
                f"Waiting {wait_time} seconds. Attempt {attempt}/{max_attempts}."
            )

            time.sleep(wait_time)
            backoff_seconds *= 2
            continue

        if 500 <= response.status_code <= 599:
            logger.warning(
                f"Server error {response.status_code} on {method} {url}. "
                f"Attempt {attempt}/{max_attempts}. Waiting {backoff_seconds} seconds."
            )

            time.sleep(backoff_seconds)
            backoff_seconds *= 2
            continue

        return response

    return response


def webex_headers():
    return {
        "Authorization": f"Bearer {WEBEX_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def te_headers():
    return {
        "Authorization": f"Bearer {THOUSANDEYES_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/hal+json"
    }


def get_webex_devices(logger, org_id=None):
    """
    Retrieves devices from Webex Control Hub.
    """
    devices = []

    url = f"{WEBEX_BASE_URL}/devices"
    params = {}

    if org_id:
        params["orgId"] = org_id

    while url:
        response = request_with_retries(
            "GET",
            url,
            headers=webex_headers(),
            logger=logger,
            params=params
        )

        if not response.ok:
            raise RuntimeError(
                f"Failed to retrieve Webex devices. "
                f"HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        items = data.get("items", [])
        devices.extend(items)

        logger.info(f"Retrieved {len(items)} Webex devices from current page.")

        url = get_next_url(response, data)

        # Only send params on the first request.
        # Next URL should already include pagination query parameters.
        params = {}

    logger.info(f"Total Webex devices retrieved: {len(devices)}")

    return devices


def get_webex_workspace_name(workspace_id, logger):
    """
    Retrieves a Workspace display name from Webex Control Hub.
    """
    url = f"{WEBEX_BASE_URL}/workspaces/{workspace_id}"

    response = request_with_retries(
        "GET",
        url,
        headers=webex_headers(),
        logger=logger
    )

    if not response.ok:
        raise RuntimeError(
            f"Failed to retrieve Webex Workspace {workspace_id}. "
            f"HTTP {response.status_code}: {response.text}"
        )

    workspace = response.json()

    return (
        workspace.get("displayName")
        or workspace.get("name")
        or workspace.get("workspaceName")
    )


def extract_webex_device_mac(device):
    """
    Attempts to extract a MAC address from common Webex device fields.
    """
    possible_mac_fields = [
        "mac",
        "macAddress",
        "networkAddress"
    ]

    for field in possible_mac_fields:
        mac = normalize_mac(device.get(field))
        if mac:
            return mac

    return None


def extract_webex_workspace_id(device):
    """
    Attempts to extract the Workspace ID from common Webex device fields.
    """
    return (
        device.get("workspaceId")
        or device.get("placeId")
        or device.get("workspace")
    )


def build_mac_to_workspace_map(logger, org_id=None):
    """
    Builds dictionary:

      {
        "AABBCCDDEEFF": "Workspace Name"
      }
    """
    devices = get_webex_devices(logger, org_id=org_id)

    workspace_cache = {}
    mac_to_workspace = {}

    skipped_no_mac = 0
    skipped_no_workspace = 0
    skipped_empty_workspace_name = 0
    skipped_duplicate_mac = 0

    for device in devices:
        device_id = device.get("id", "unknown")
        device_display_name = device.get("displayName", "unknown")

        mac = extract_webex_device_mac(device)

        if not mac:
            skipped_no_mac += 1
            logger.info(
                f"Skipping Webex device without valid MAC. "
                f"Device ID: {device_id}, Display Name: {device_display_name}"
            )
            continue

        workspace_id = extract_webex_workspace_id(device)

        if not workspace_id:
            skipped_no_workspace += 1
            logger.info(
                f"Skipping Webex device without Workspace ID. "
                f"Device ID: {device_id}, MAC: {mac}, Display Name: {device_display_name}"
            )
            continue

        if workspace_id not in workspace_cache:
            workspace_name = get_webex_workspace_name(workspace_id, logger)
            workspace_cache[workspace_id] = workspace_name
        else:
            workspace_name = workspace_cache[workspace_id]

        if not workspace_name:
            skipped_empty_workspace_name += 1
            logger.warning(
                f"Skipping Webex device because Workspace name is empty. "
                f"Device ID: {device_id}, MAC: {mac}, Workspace ID: {workspace_id}"
            )
            continue

        if mac in mac_to_workspace:
            skipped_duplicate_mac += 1
            logger.warning(
                f"Duplicate MAC found in Webex device list: {mac}. "
                f"Keeping existing Workspace mapping: '{mac_to_workspace[mac]}'. "
                f"Ignoring new mapping to: '{workspace_name}'."
            )
            continue

        mac_to_workspace[mac] = workspace_name

        logger.info(
            f"Mapped Webex MAC {mac} to Workspace '{workspace_name}'."
        )

    logger.info("Webex mapping summary:")
    logger.info(f"  Webex MAC-to-Workspace mappings created: {len(mac_to_workspace)}")
    logger.info(f"  Webex devices skipped due to missing MAC: {skipped_no_mac}")
    logger.info(f"  Webex devices skipped due to missing Workspace: {skipped_no_workspace}")
    logger.info(f"  Webex devices skipped due to empty Workspace name: {skipped_empty_workspace_name}")
    logger.info(f"  Webex devices skipped due to duplicate MAC: {skipped_duplicate_mac}")

    return mac_to_workspace


def extract_te_agents_from_response(data):
    """
    Extracts Endpoint Agents from common ThousandEyes v7/HAL response shapes.
    """
    if not isinstance(data, dict):
        return []

    # Common direct response shapes
    if isinstance(data.get("agents"), list):
        return data.get("agents")

    if isinstance(data.get("endpointAgents"), list):
        return data.get("endpointAgents")

    if isinstance(data.get("items"), list):
        return data.get("items")

    # Common HAL response shape
    embedded = data.get("_embedded")

    if isinstance(embedded, dict):
        if isinstance(embedded.get("agents"), list):
            return embedded.get("agents")

        if isinstance(embedded.get("endpointAgents"), list):
            return embedded.get("endpointAgents")

        if isinstance(embedded.get("items"), list):
            return embedded.get("items")

    return []


def get_thousandeyes_endpoint_agents(logger):
    """
    Retrieves ThousandEyes Endpoint Agents only.

    Uses:
      GET /v7/endpoint/agents
    """
    endpoint_agents = []

    url = TE_ENDPOINT_AGENTS_URL
    params = {}

    while url:
        response = request_with_retries(
            "GET",
            url,
            headers=te_headers(),
            logger=logger,
            params=params
        )

        if not response.ok:
            raise RuntimeError(
                f"Failed to retrieve ThousandEyes Endpoint Agents. "
                f"HTTP {response.status_code}: {response.text}"
            )

        data = response.json()

        page_agents = extract_te_agents_from_response(data)
        endpoint_agents.extend(page_agents)

        logger.info(f"Retrieved {len(page_agents)} ThousandEyes Endpoint Agents from current page.")

        url = get_next_url(response, data)

        # Only send params on first request.
        params = {}

    logger.info(f"Total ThousandEyes Endpoint Agents retrieved: {len(endpoint_agents)}")

    return endpoint_agents


def extract_te_endpoint_agent_id(agent):
    return (
        agent.get("agentId")
        or agent.get("id")
        or agent.get("agentID")
    )


def extract_te_endpoint_agent_name(agent):
    """
    For Endpoint Agents, the documented editable field is 'name'.
    'agentName' is retained only as a fallback for older/alternate response shapes.
    """
    return (
        agent.get("name")
        or agent.get("agentName")
    )


def extract_single_te_agent_from_response(data):
    """
    Extracts a single Endpoint Agent from common response shapes.
    """
    if not isinstance(data, dict):
        return None

    if isinstance(data.get("agent"), dict):
        return data.get("agent")

    if isinstance(data.get("endpointAgent"), dict):
        return data.get("endpointAgent")

    embedded = data.get("_embedded")

    if isinstance(embedded, dict):
        if isinstance(embedded.get("agent"), dict):
            return embedded.get("agent")

        if isinstance(embedded.get("endpointAgent"), dict):
            return embedded.get("endpointAgent")

    # If the response object itself has name/id fields, treat it as the agent.
    if "name" in data or "agentName" in data or "agentId" in data or "id" in data:
        return data

    return None


def get_thousandeyes_endpoint_agent_by_id(agent_id, logger):
    """
    Retrieves a single ThousandEyes Endpoint Agent by ID.

    Uses:
      GET /v7/endpoint/agents/{agentId}
    """
    url = f"{TE_ENDPOINT_AGENTS_URL}/{agent_id}"

    response = request_with_retries(
        "GET",
        url,
        headers=te_headers(),
        logger=logger
    )

    if not response.ok:
        logger.warning(
            f"Could not retrieve ThousandEyes Endpoint Agent ID {agent_id}. "
            f"HTTP {response.status_code}: {response.text}"
        )
        return None

    data = response.json()
    return extract_single_te_agent_from_response(data)


def update_thousandeyes_endpoint_agent_name(agent, new_name, logger):
    """
    Updates ThousandEyes Endpoint Agent name using the documented endpoint:

      PATCH /v7/endpoint/agents/{agentId}

    With payload:

      {
        "name": "Workspace Name",
        "licenseType": "advantage"
      }

    Returns:
      True if the change is verified by a follow-up GET.
      False otherwise.
    """
    agent_id = extract_te_endpoint_agent_id(agent)

    if not agent_id:
        logger.error("Cannot update Endpoint Agent because agent ID is missing.")
        return False

    url = f"{TE_ENDPOINT_AGENTS_URL}/{agent_id}"

    payload = {
        "name": new_name,
        "licenseType": TE_LICENSE_TYPE
    }

    logger.info(
        f"Sending PATCH to ThousandEyes Endpoint Agent ID {agent_id}. "
        f"Payload: {payload}"
    )

    response = request_with_retries(
        "PATCH",
        url,
        headers=te_headers(),
        logger=logger,
        json=payload
    )

    logger.info(
        f"PATCH response for Endpoint Agent ID {agent_id}: "
        f"HTTP {response.status_code}: {response.text}"
    )

    if not response.ok:
        logger.error(
            f"FAILED: PATCH request failed for Endpoint Agent ID {agent_id}. "
            f"HTTP {response.status_code}: {response.text}"
        )
        return False

    # Verify the change. Try several times in case the backend/UI/API takes a moment to reflect it.
    verification_attempts = 5
    wait_seconds = 3

    for attempt in range(1, verification_attempts + 1):
        logger.info(
            f"Verifying Endpoint Agent ID {agent_id} name change. "
            f"Attempt {attempt}/{verification_attempts}."
        )

        time.sleep(wait_seconds)

        refreshed_agent = get_thousandeyes_endpoint_agent_by_id(agent_id, logger)

        if not refreshed_agent:
            logger.warning(
                f"Could not retrieve Endpoint Agent ID {agent_id} during verification attempt {attempt}."
            )
            continue

        refreshed_name = extract_te_endpoint_agent_name(refreshed_agent)

        logger.info(
            f"Verification for Endpoint Agent ID {agent_id}: "
            f"current API-reported name is '{refreshed_name}', expected '{new_name}'."
        )

        if refreshed_name == new_name:
            logger.info(
                f"VERIFIED: Endpoint Agent ID {agent_id} renamed to '{new_name}'."
            )
            return True

    logger.error(
        f"PATCH returned success, but Endpoint Agent ID {agent_id} name was not verified as updated. "
        f"Expected name: '{new_name}'."
    )

    return False


def rename_matching_te_endpoint_agents(mac_to_workspace, logger, apply_changes=False, debug_names=False):
    endpoint_agents = get_thousandeyes_endpoint_agents(logger)

    total_endpoint_agents = len(endpoint_agents)
    sep_named_agents = 0
    matched_agents = 0
    renamed_agents = 0
    failed_updates = 0
    skipped_no_match_in_webex = 0
    skipped_missing_id = 0
    skipped_missing_name = 0

    logger.info("Starting ThousandEyes Endpoint Agent rename evaluation.")
    logger.info(f"Apply mode: {apply_changes}")
    logger.info(f"Using hardcoded Endpoint Agent licenseType for PATCH: {TE_LICENSE_TYPE}")

    if debug_names:
        logger.info("Debug mode enabled: listing Endpoint Agent IDs and names returned by API.")
        for agent in endpoint_agents:
            agent_id = extract_te_endpoint_agent_id(agent)
            current_name = extract_te_endpoint_agent_name(agent)
            logger.info(f"Endpoint Agent from API - ID: {agent_id}, Name: {current_name}")

    for agent in endpoint_agents:
        agent_id = extract_te_endpoint_agent_id(agent)
        current_name = extract_te_endpoint_agent_name(agent)

        if not current_name:
            skipped_missing_name += 1
            logger.info(
                f"Skipping Endpoint Agent because no agent name was found. "
                f"Agent ID: {agent_id}, Available fields: {list(agent.keys())}"
            )
            continue

        current_name = str(current_name).strip()

        match = SEP_MAC_PATTERN.fullmatch(current_name)

        # Only touch Endpoint Agents whose name is exactly SEPAABBCCDDEEFF.
        if not match:
            continue

        sep_named_agents += 1

        mac_from_te_name = match.group(1).upper()

        if mac_from_te_name not in mac_to_workspace:
            skipped_no_match_in_webex += 1
            logger.info(
                f"Skipping Endpoint Agent '{current_name}' because MAC {mac_from_te_name} "
                f"does not match any Webex Control Hub device."
            )
            continue

        matched_agents += 1
        target_workspace_name = mac_to_workspace[mac_from_te_name]

        if not agent_id:
            skipped_missing_id += 1
            logger.warning(
                f"Skipping Endpoint Agent '{current_name}' because no agent ID was found in API response."
            )
            continue

        if apply_changes:
            logger.info(
                f"Renaming ThousandEyes Endpoint Agent ID {agent_id}: "
                f"'{current_name}' -> '{target_workspace_name}'"
            )

            verified_success = update_thousandeyes_endpoint_agent_name(
                agent=agent,
                new_name=target_workspace_name,
                logger=logger
            )

            if verified_success:
                renamed_agents += 1
                logger.info(
                    f"SUCCESS VERIFIED: Renamed ThousandEyes Endpoint Agent ID {agent_id}: "
                    f"'{current_name}' -> '{target_workspace_name}'"
                )
            else:
                failed_updates += 1
                logger.error(
                    f"FAILED OR NOT VERIFIED: Could not confirm rename of ThousandEyes Endpoint Agent ID {agent_id}: "
                    f"'{current_name}' -> '{target_workspace_name}'."
                )
        else:
            logger.info(
                f"DRY RUN: Would PATCH ThousandEyes Endpoint Agent ID {agent_id}: "
                f"'{current_name}' -> '{target_workspace_name}' "
                f"with payload {{'name': '{target_workspace_name}', 'licenseType': '{TE_LICENSE_TYPE}'}}"
            )

    logger.info("ThousandEyes Endpoint Agent rename evaluation completed.")
    logger.info("Summary:")
    logger.info(f"  Total TE Endpoint Agents retrieved: {total_endpoint_agents}")
    logger.info(f"  TE Endpoint Agents with SEP<MAC> name: {sep_named_agents}")
    logger.info(f"  SEP<MAC> TE Endpoint Agents matched to Webex MAC: {matched_agents}")
    logger.info(f"  TE Endpoint Agents renamed and verified: {renamed_agents}")
    logger.info(f"  TE Endpoint Agent update failures or unverified updates: {failed_updates}")
    logger.info(f"  SEP<MAC> TE Endpoint Agents skipped due to no Webex MAC match: {skipped_no_match_in_webex}")
    logger.info(f"  SEP<MAC> TE Endpoint Agents skipped due to missing TE Agent ID: {skipped_missing_id}")
    logger.info(f"  TE Endpoint Agents skipped due to missing name field: {skipped_missing_name}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Rename ThousandEyes Endpoint Agents named SEPAABBCCDDEEFF to their associated "
            "Webex Control Hub Workspace name."
        )
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually apply the ThousandEyes Endpoint Agent name changes. "
            "Without this, the script runs in dry-run mode."
        )
    )

    parser.add_argument(
        "--org-id",
        help="Optional Webex orgId. Useful if your token can access multiple organizations."
    )

    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional log file path. Default: te_endpoint_webex_rename_YYYYMMDD_HHMMSS.txt"
    )

    parser.add_argument(
        "--debug-names",
        action="store_true",
        help=(
            "Print all ThousandEyes Endpoint Agent IDs and names returned by the API. "
            "Useful to confirm the API is returning the expected SEP<MAC> names."
        )
    )

    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = args.log_file or f"te_endpoint_webex_rename_{timestamp}.txt"

    logger = setup_logging(log_file)

    logger.info("Starting Webex Control Hub to ThousandEyes Endpoint Agent rename script.")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Using ThousandEyes Endpoint Agents API: {TE_ENDPOINT_AGENTS_URL}")
    logger.info(f"Using ThousandEyes PATCH licenseType: {TE_LICENSE_TYPE}")

    if args.apply:
        logger.info("Running in APPLY mode. Matching ThousandEyes Endpoint Agents will be renamed.")
    else:
        logger.info("Running in DRY-RUN mode. No ThousandEyes Endpoint Agents will be renamed.")
        logger.info("Use --apply to actually perform the rename.")

    try:
        validate_tokens()

        mac_to_workspace = build_mac_to_workspace_map(
            logger=logger,
            org_id=args.org_id
        )

        rename_matching_te_endpoint_agents(
            mac_to_workspace=mac_to_workspace,
            logger=logger,
            apply_changes=args.apply,
            debug_names=args.debug_names
        )

        logger.info("Script completed.")

    except Exception as exc:
        logger.exception(f"Script failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
