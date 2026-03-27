#!/usr/bin/env python3
"""
Build fleet_data.json from Oracle Cloud Infrastructure.

This script is designed to keep the browser app static while moving OCI access
into a repeatable collector job. It preserves the current dashboard schema and
adds richer reboot fields for future UI work.

Examples:
  python3 build_fleet_data.py
  python3 build_fleet_data.py --profile MYPROFILE --output fleet_data.json
  python3 build_fleet_data.py --auth instance_principal --customer-strategy compartment

Requirements:
  pip install oci
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import oci
    from oci.pagination import list_call_get_all_results
except ImportError:  # pragma: no cover - handled at runtime
    oci = None
    list_call_get_all_results = None


UTC = timezone.utc
DEFAULT_OUTPUT = "fleet_data.json"
EVENT_PREFIX = "__OTX_EVENT__ "
DEFAULT_TAG_KEYS = [
    "Customer",
    "customer",
    "CustomerName",
    "customer_name",
    "Customer_Name",
]


def log(message: str) -> None:
    print(message, flush=True)


def emit_event(event_type: str, **payload: Any) -> None:
    print(f"{EVENT_PREFIX}{json.dumps({'type': event_type, **payload})}", flush=True)


@dataclass
class CollectorArgs:
    auth: str
    config_file: str
    profile: str
    output: Path
    customer_strategy: str
    customer_tag_keys: list[str]
    include_terminated: bool
    maintenance_timeout_seconds: float
    osmh_timeout_seconds: float
    compute_timeout_seconds: float
    max_region_workers: int


def parse_args() -> CollectorArgs:
    parser = argparse.ArgumentParser(description="Build a tenancy-wide fleet_data.json from OCI.")
    parser.add_argument("--auth", choices=["config", "instance_principal"], default="config")
    parser.add_argument("--config-file", default="~/.oci/config")
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--customer-strategy",
        choices=["tag", "compartment", "tenancy"],
        default="tenancy",
        help="How instances should be grouped into dashboard customer cards.",
    )
    parser.add_argument(
        "--customer-tag-keys",
        default=",".join(DEFAULT_TAG_KEYS),
        help="Comma-separated tag keys to check when customer strategy is 'tag'.",
    )
    parser.add_argument(
        "--include-terminated",
        action="store_true",
        help="Include terminated and terminating instances in the output.",
    )
    parser.add_argument(
        "--maintenance-timeout-seconds",
        type=float,
        default=20.0,
        help="Maximum seconds to wait per-compartment for instance maintenance events before continuing.",
    )
    parser.add_argument(
        "--osmh-timeout-seconds",
        type=float,
        default=20.0,
        help="Maximum seconds to wait per-region for OS Management Hub boot data before continuing.",
    )
    parser.add_argument(
        "--compute-timeout-seconds",
        type=float,
        default=20.0,
        help="Maximum seconds to wait per-compartment for compute instance listing before continuing.",
    )
    parser.add_argument(
        "--max-region-workers",
        type=int,
        default=3,
        help="Maximum number of regions to process in parallel.",
    )
    ns = parser.parse_args()
    return CollectorArgs(
        auth=ns.auth,
        config_file=ns.config_file,
        profile=ns.profile,
        output=Path(ns.output).expanduser(),
        customer_strategy=ns.customer_strategy,
        customer_tag_keys=[item.strip() for item in ns.customer_tag_keys.split(",") if item.strip()],
        include_terminated=ns.include_terminated,
        maintenance_timeout_seconds=ns.maintenance_timeout_seconds,
        osmh_timeout_seconds=ns.osmh_timeout_seconds,
        compute_timeout_seconds=ns.compute_timeout_seconds,
        max_region_workers=max(1, ns.max_region_workers),
    )


def require_oci_sdk() -> None:
    if oci is None:
        raise SystemExit(
            "The OCI Python SDK is not installed. Run `python3 -m pip install oci` and try again."
        )


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_utc_display(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def shift_display(dt: datetime | None, hours: int, minutes: int = 0) -> str:
    if dt is None:
        return "-"
    shifted = dt.astimezone(UTC) + timedelta(hours=hours, minutes=minutes)
    return shifted.strftime("%Y-%m-%d %H:%M")


def parse_any_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text == "-":
        return None
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    if " " in text and "T" not in text:
        candidates.append(text.replace(" ", "T"))
        candidates.append(text.replace(" ", "T") + "Z")
        candidates.append(text.replace(" ", "T") + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def floor_to_minute(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass
class MaintenanceEventDetails:
    event_id: str
    instance_id: str
    instance_action: str
    lifecycle_state: str
    maintenance_category: str
    maintenance_reason: str
    time_window_start: datetime | None
    time_hard_due_date: datetime | None
    time_created: datetime | None


ACTIVE_MAINTENANCE_STATES = {"SCHEDULED", "STARTED", "PROCESSING"}
ACTION_PRIORITY = {
    "REBOOT_MIGRATION": 0,
    "STOP": 1,
    "TERMINATE": 2,
    "NONE": 3,
    "UNKNOWN_ENUM_VALUE": 4,
}
STATE_PRIORITY = {
    "STARTED": 0,
    "PROCESSING": 1,
    "SCHEDULED": 2,
    "SUCCEEDED": 3,
    "CANCELED": 4,
    "FAILED": 5,
    "UNKNOWN_ENUM_VALUE": 6,
}


def read_previous_output(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}, {}
    by_id = {
        row["ID"]: row
        for row in payload.get("instances", [])
        if isinstance(row, dict) and row.get("ID")
    }
    return by_id, payload


def load_signer_and_config(args: CollectorArgs) -> tuple[dict[str, Any], Any]:
    require_oci_sdk()
    if args.auth == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {"region": signer.region}
        return config, signer
    config = oci.config.from_file(file_location=args.config_file, profile_name=args.profile)
    return config, None


def build_identity_client(config: dict[str, Any], signer: Any):
    kwargs = {"config": config}
    if signer is not None:
        kwargs["signer"] = signer
    return oci.identity.IdentityClient(**kwargs)


def build_compute_client(config: dict[str, Any], signer: Any):
    kwargs = {"config": config}
    if signer is not None:
        kwargs["signer"] = signer
    return oci.core.ComputeClient(**kwargs)


def build_maintenance_event_client(config: dict[str, Any], signer: Any):
    return build_compute_client(config, signer)


def maybe_build_osmh_client(config: dict[str, Any], signer: Any):
    kwargs = {"config": config}
    if signer is not None:
        kwargs["signer"] = signer
    if not hasattr(oci, "os_management_hub"):
        return None
    client_cls = getattr(oci.os_management_hub, "ManagedInstanceClient", None)
    if client_cls is None:
        client_cls = getattr(oci.os_management_hub, "OsManagementHubClient", None)
    if client_cls is None:
        return None
    try:
        return client_cls(**kwargs)
    except Exception:
        return None


def get_tenancy_id(config: dict[str, Any], signer: Any) -> str:
    if config.get("tenancy"):
        return config["tenancy"]
    if signer is not None and getattr(signer, "tenancy_id", None):
        return signer.tenancy_id
    raise RuntimeError("Unable to determine tenancy OCID from config or signer.")


def list_compartments(identity_client: Any, tenancy_id: str) -> dict[str, dict[str, str]]:
    compartments = {
        tenancy_id: {
            "id": tenancy_id,
            "name": "Tenancy Root",
            "parent_id": "",
        }
    }
    response = list_call_get_all_results(
        identity_client.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    for compartment in response.data:
        if getattr(compartment, "lifecycle_state", "") != "ACTIVE":
            continue
        compartments[compartment.id] = {
            "id": compartment.id,
            "name": compartment.name,
            "parent_id": compartment.compartment_id,
        }
    return compartments


def list_regions(identity_client: Any, tenancy_id: str) -> list[str]:
    response = identity_client.list_region_subscriptions(tenancy_id)
    return [row.region_name for row in response.data if getattr(row, "status", "READY") == "READY"]


def list_instances_for_region(
    base_config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
    timeout_seconds: float,
) -> tuple[list[Any], list[str]]:
    regional_config = dict(base_config)
    regional_config["region"] = region
    client = build_compute_client(regional_config, signer)
    results: list[Any] = []
    warnings: list[str] = []
    workers: list[tuple[str, str, dict[str, Any], float, threading.Thread]] = []
    for compartment_id in compartments:
        compartment_name = compartments.get(compartment_id, {}).get("name", compartment_id)
        response_holder: dict[str, Any] = {"response": None, "error": None}
        start = time.time()
        warnings.append(
            f"Calling Compute list_instances in {region} for compartment {compartment_name} ({compartment_id}) "
            f"via {client.base_client.endpoint}"
        )

        def worker(
            compartment_id: str = compartment_id,
            response_holder: dict[str, Any] = response_holder,
        ) -> None:
            try:
                response_holder["response"] = list_call_get_all_results(
                    client.list_instances,
                    compartment_id=compartment_id,
                )
            except Exception as exc:
                response_holder["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        workers.append((compartment_id, compartment_name, response_holder, start, thread))

    for compartment_id, compartment_name, response_holder, start, thread in workers:
        thread.join(timeout_seconds)
        if thread.is_alive():
            warnings.append(
                f"Compute listing in {region} exceeded {timeout_seconds:g}s for compartment "
                f"{compartment_name} ({compartment_id}); skipping"
            )
            continue
        if response_holder["error"] is not None:
            elapsed = time.time() - start
            warnings.append(
                f"Compute listing failed in {region} for compartment {compartment_name} "
                f"({compartment_id}) after {elapsed:.2f}s: {response_holder['error']}"
            )
            continue

        response = response_holder["response"]
        if response is None or getattr(response, "data", None) is None:
            elapsed = time.time() - start
            warnings.append(
                f"Compute listing returned no data in {region} for compartment {compartment_name} "
                f"({compartment_id}) after {elapsed:.2f}s; skipping"
            )
            continue
        elapsed = time.time() - start
        warnings.append(
            f"Compute listing completed in {region} for compartment {compartment_name} "
            f"({compartment_id}) in {elapsed:.2f}s with {len(response.data)} instances"
        )
        results.extend(response.data)
    return results, warnings


def choose_best_maintenance_event(events: list[Any]) -> MaintenanceEventDetails | None:
    candidates: list[MaintenanceEventDetails] = []
    for event in events:
        lifecycle_state = getattr(event, "lifecycle_state", "") or ""
        if lifecycle_state and lifecycle_state not in ACTIVE_MAINTENANCE_STATES:
            continue
        instance_id = getattr(event, "instance_id", None)
        if not instance_id:
            continue
        candidates.append(
            MaintenanceEventDetails(
                event_id=getattr(event, "id", "") or "",
                instance_id=instance_id,
                instance_action=(getattr(event, "instance_action", "") or "UNKNOWN_ENUM_VALUE"),
                lifecycle_state=lifecycle_state or "UNKNOWN_ENUM_VALUE",
                maintenance_category=getattr(event, "maintenance_category", "") or "",
                maintenance_reason=getattr(event, "maintenance_reason", "") or "",
                time_window_start=parse_any_datetime(getattr(event, "time_window_start", None)),
                time_hard_due_date=parse_any_datetime(getattr(event, "time_hard_due_date", None)),
                time_created=parse_any_datetime(getattr(event, "time_created", None)),
            )
        )

    if not candidates:
        return None

    def sort_key(event: MaintenanceEventDetails) -> tuple[int, int, datetime]:
        event_time = event.time_hard_due_date or event.time_window_start or event.time_created or datetime.max.replace(tzinfo=UTC)
        return (
            STATE_PRIORITY.get(event.lifecycle_state, 99),
            ACTION_PRIORITY.get(event.instance_action, 99),
            event_time,
        )

    return sorted(candidates, key=sort_key)[0]


def list_maintenance_events_for_region(
    base_config: dict[str, Any],
    signer: Any,
    region: str,
    compartment_ids: list[str],
    timeout_seconds: float,
) -> tuple[dict[str, MaintenanceEventDetails], list[str]]:
    if not compartment_ids:
        return {}, []

    regional_config = dict(base_config)
    regional_config["region"] = region
    client = build_maintenance_event_client(regional_config, signer)
    list_method = getattr(client, "list_instance_maintenance_events", None)
    if list_method is None:
        return {}, [f"Instance maintenance events API is not available in the current OCI SDK for {region}; falling back to instance metadata."]

    warnings: list[str] = []
    grouped_events: dict[str, list[Any]] = defaultdict(list)
    workers: list[tuple[str, dict[str, Any], float, threading.Thread]] = []

    for compartment_id in compartment_ids:
        response_holder: dict[str, Any] = {"response": None, "error": None}
        start = time.time()
        warnings.append(
            f"Calling Compute list_instance_maintenance_events in {region} for compartment {compartment_id} "
            f"via {client.base_client.endpoint}"
        )

        def worker(
            compartment_id: str = compartment_id,
            response_holder: dict[str, Any] = response_holder,
        ) -> None:
            try:
                response_holder["response"] = list_call_get_all_results(
                    list_method,
                    compartment_id=compartment_id,
                )
            except Exception as exc:
                response_holder["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        workers.append((compartment_id, response_holder, start, thread))

    for compartment_id, response_holder, start, thread in workers:
        thread.join(timeout_seconds)
        if thread.is_alive():
            warnings.append(
                f"Instance maintenance event lookup in {region} exceeded {timeout_seconds:g}s for compartment "
                f"{compartment_id}; continuing with instance metadata fallback"
            )
            continue
        if response_holder["error"] is not None:
            elapsed = time.time() - start
            warnings.append(
                f"Instance maintenance event lookup failed in {region} for compartment {compartment_id} "
                f"after {elapsed:.2f}s: {response_holder['error']}"
            )
            continue

        response = response_holder["response"]
        items = [] if response is None or getattr(response, "data", None) is None else response.data
        elapsed = time.time() - start
        warnings.append(
            f"Instance maintenance event lookup completed in {region} for compartment {compartment_id} "
            f"in {elapsed:.2f}s with {len(items)} events"
        )
        for event in items:
            instance_id = getattr(event, "instance_id", None)
            if instance_id:
                grouped_events[instance_id].append(event)

    selected = {
        instance_id: best
        for instance_id, best in (
            (instance_id, choose_best_maintenance_event(events))
            for instance_id, events in grouped_events.items()
        )
        if best is not None
    }
    return selected, warnings


def list_osmh_last_boots(
    base_config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
) -> dict[tuple[str, str], datetime]:
    regional_config = dict(base_config)
    regional_config["region"] = region
    client = maybe_build_osmh_client(regional_config, signer)
    if client is None:
        return {}

    boots: dict[tuple[str, str], datetime] = {}
    list_method = getattr(client, "list_managed_instances", None)
    if list_method is None:
        return {}

    for compartment_id in compartments:
        try:
            response = list_call_get_all_results(list_method, compartment_id=compartment_id)
        except Exception:
            continue
        for managed in response.data:
            display_name = getattr(managed, "display_name", None)
            time_last_boot = parse_any_datetime(getattr(managed, "time_last_boot", None))
            instance_compartment_id = getattr(managed, "compartment_id", compartment_id)
            if display_name and time_last_boot:
                boots[(instance_compartment_id, display_name)] = time_last_boot
    return boots


def list_osmh_last_boots_with_timeout(
    base_config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
    timeout_seconds: float,
) -> tuple[dict[tuple[str, str], datetime], bool]:
    regional_config = dict(base_config)
    regional_config["region"] = region
    client = maybe_build_osmh_client(regional_config, signer)
    if client is None:
        return {}, False

    list_method = getattr(client, "list_managed_instances", None)
    if list_method is None:
        return {}, False

    boots: dict[tuple[str, str], datetime] = {}
    timed_out = False
    workers: list[tuple[str, dict[str, Any], threading.Thread]] = []

    for compartment_id in compartments:
        result: dict[str, Any] = {"items": [], "error": None}

        def worker(compartment_id: str = compartment_id, result: dict[str, Any] = result) -> None:
            try:
                response = list_call_get_all_results(list_method, compartment_id=compartment_id)
                result["items"] = response.data
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        workers.append((compartment_id, result, thread))

    for compartment_id, result, thread in workers:
        thread.join(timeout_seconds)
        if thread.is_alive():
            timed_out = True
            continue
        if result["error"] is not None:
            continue
        for managed in result["items"]:
            display_name = getattr(managed, "display_name", None)
            time_last_boot = parse_any_datetime(getattr(managed, "time_last_boot", None))
            instance_compartment_id = getattr(managed, "compartment_id", compartment_id)
            if display_name and time_last_boot:
                boots[(instance_compartment_id, display_name)] = time_last_boot

    return boots, timed_out


def flatten_defined_tags(tags: dict[str, Any] | None) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for namespace, values in (tags or {}).items():
        if isinstance(values, dict):
            for key, value in values.items():
                flat[f"{namespace}.{key}"] = value
        else:
            flat[namespace] = values
    return flat


def choose_customer_id(
    args: CollectorArgs,
    tenancy_name: str,
    compartment_name: str,
    freeform_tags: dict[str, Any],
    defined_tags: dict[str, Any],
) -> str:
    if args.customer_strategy == "tenancy":
        return tenancy_name
    if args.customer_strategy == "compartment":
        return compartment_name or tenancy_name

    flat_defined = flatten_defined_tags(defined_tags)
    for tag_key in args.customer_tag_keys:
        if freeform_tags.get(tag_key):
            return str(freeform_tags[tag_key]).strip()
        if flat_defined.get(tag_key):
            return str(flat_defined[tag_key]).strip()
        for full_key, value in flat_defined.items():
            if full_key.split(".")[-1] == tag_key and value:
                return str(value).strip()
    return compartment_name or tenancy_name


def maintenance_action_requires_reboot(action: str) -> bool:
    return action in {"REBOOT_MIGRATION", "STOP", "TERMINATE"}


def format_maintenance_action(action: str) -> str:
    mapping = {
        "REBOOT_MIGRATION": "Reboot Migration",
        "STOP": "Stop Instance",
        "TERMINATE": "Terminate Instance",
        "NONE": "Live / No Customer Action",
        "UNKNOWN_ENUM_VALUE": "Unknown",
        "": "",
    }
    return mapping.get(action, action.replace("_", " ").title())


def format_maintenance_state(state: str) -> str:
    if not state:
        return ""
    return state.replace("_", " ").title()


def format_live_migration_preference(instance: Any) -> str:
    availability_config = getattr(instance, "availability_config", None)
    preference = getattr(availability_config, "is_live_migration_preferred", None)
    if preference is True:
        return "Use Live Migration If Possible"
    if preference is False:
        return "Opt Out"
    return "Oracle Selects Best Option"


def derive_reboot_fields(
    maintenance_due_dt: datetime | None,
    maintenance_action: str,
    last_boot_dt: datetime | None,
    previous: dict[str, Any] | None,
) -> tuple[str, str, str]:
    previous = previous or {}
    if maintenance_due_dt is not None and maintenance_action_requires_reboot(maintenance_action):
        last_reboot_utc = format_utc_display(last_boot_dt) if last_boot_dt else previous.get("Last_Reboot_UTC", "")
        evidence = "OS Management Hub" if last_boot_dt else (previous.get("Reboot_Evidence") or "-")
        return "Scheduled", last_reboot_utc, evidence

    if last_boot_dt is not None:
        evidence = "OS Management Hub"
        if previous.get("Maintenance_UTC"):
            return "Completed", format_utc_display(last_boot_dt), evidence
        return "Not Scheduled", format_utc_display(last_boot_dt), evidence

    if previous.get("reboot_status") in {"Scheduled", "Completed"}:
        return "Completed", previous.get("Last_Reboot_UTC", ""), previous.get("Reboot_Evidence") or "Maintenance cleared from snapshot diff"

    return "Not Scheduled", previous.get("Last_Reboot_UTC", ""), previous.get("Reboot_Evidence", "-")


def derive_status_change_fields(
    previous: dict[str, Any] | None,
    reboot_status: str,
    maintenance_action: str,
    maintenance_due_dt: datetime | None,
    maintenance_event_state: str,
    generated_at: datetime,
) -> tuple[str, str, str]:
    previous = previous or {}
    previous_status = previous.get("reboot_status", "")
    if previous_status and previous_status != reboot_status:
        return (
            previous_status,
            f"{previous_status} -> {reboot_status}",
            format_utc_display(generated_at),
        )
    previous_signature = "|".join(
        [
            previous.get("Maintenance_Action", ""),
            previous.get("Maintenance_UTC", ""),
            previous.get("Maintenance_Event_Status", ""),
        ]
    )
    current_signature = "|".join(
        [
            format_maintenance_action(maintenance_action),
            format_utc_display(maintenance_due_dt),
            format_maintenance_state(maintenance_event_state),
        ]
    )
    if current_signature != previous_signature and any(part for part in current_signature.split("|")):
        return previous_status or "-", "Maintenance Updated", format_utc_display(generated_at)
    return previous_status or "-", "Stable", ""


def build_instance_row(
    args: CollectorArgs,
    instance: Any,
    region: str,
    tenancy_id: str,
    tenancy_name: str,
    generated_at: datetime,
    compartments: dict[str, dict[str, str]],
    maintenance_events: dict[str, MaintenanceEventDetails],
    last_boots: dict[tuple[str, str], datetime],
    previous_rows: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    state = getattr(instance, "lifecycle_state", "") or ""
    if not args.include_terminated and state.upper() in {"TERMINATED", "TERMINATING"}:
        return None

    instance_id = getattr(instance, "id", None)
    display_name = getattr(instance, "display_name", None) or instance_id
    compartment_id = getattr(instance, "compartment_id", "")
    compartment_name = compartments.get(compartment_id, {}).get("name", "Unknown")
    maintenance_event = maintenance_events.get(instance_id)
    maintenance_action = maintenance_event.instance_action if maintenance_event else ""
    maintenance_event_state = maintenance_event.lifecycle_state if maintenance_event else ""
    maintenance_due_dt = (
        maintenance_event.time_hard_due_date
        if maintenance_event and maintenance_event.time_hard_due_date
        else maintenance_event.time_window_start
        if maintenance_event and maintenance_event.time_window_start
        else parse_any_datetime(getattr(instance, "time_maintenance_reboot_due", None))
    )
    maintenance_source = (
        "Instance Maintenance Event"
        if maintenance_event is not None
        else "Instance Summary"
        if maintenance_due_dt is not None
        else ""
    )
    effective_maintenance_action = maintenance_action
    if not effective_maintenance_action and maintenance_due_dt is not None and maintenance_source == "Instance Summary":
        effective_maintenance_action = "REBOOT_MIGRATION"
    previous = previous_rows.get(instance_id, {})
    last_boot_dt = last_boots.get((compartment_id, display_name))
    reboot_status, last_reboot_utc, reboot_evidence = derive_reboot_fields(
        maintenance_due_dt,
        effective_maintenance_action,
        last_boot_dt,
        previous,
    )
    previous_status, status_change_signal, status_changed_utc = derive_status_change_fields(
        previous,
        reboot_status,
        effective_maintenance_action,
        maintenance_due_dt,
        maintenance_event_state,
        generated_at,
    )
    last_reboot_dt = parse_any_datetime(last_reboot_utc)
    freeform_tags = getattr(instance, "freeform_tags", {}) or {}
    defined_tags = getattr(instance, "defined_tags", {}) or {}
    customer_id = choose_customer_id(args, tenancy_name, compartment_name, freeform_tags, defined_tags)

    row = {
        "customerId": customer_id,
        "ID": instance_id,
        "Display_Name": display_name,
        "Shape": getattr(instance, "shape", "") or "",
        "State": state,
        "Availability_Domain": getattr(instance, "availability_domain", "") or "",
        "Fault_Domain": getattr(instance, "fault_domain", "") or "",
        "Time_Created": format_utc_display(parse_any_datetime(getattr(instance, "time_created", None))),
        "Compartment_ID": compartment_id,
        "Compartment_Name": compartment_name,
        "Tenant_ID": tenancy_id,
        "Region": region,
        "Host_ID": getattr(instance, "dedicated_vm_host_id", "") or getattr(instance, "host_id", "") or "",
        "Maintenance_Reboot_Due": "-" if maintenance_due_dt is None or not maintenance_action_requires_reboot(effective_maintenance_action) else "Yes",
        "Maintenance_UTC": format_utc_display(maintenance_due_dt),
        "Maintenance_IST": shift_display(maintenance_due_dt, 5, 30),
        "Maintenance_EDT": shift_display(maintenance_due_dt, -4, 0),
        "Maintenance_Source": maintenance_source,
        "Maintenance_Action": format_maintenance_action(effective_maintenance_action),
        "Maintenance_Event_Status": format_maintenance_state(maintenance_event_state),
        "Maintenance_Category": maintenance_event.maintenance_category if maintenance_event else "",
        "Maintenance_Reason": maintenance_event.maintenance_reason if maintenance_event else "",
        "Maintenance_Event_OCID": maintenance_event.event_id if maintenance_event else "",
        "Live_Migration_Preference": format_live_migration_preference(instance),
        "Last_Reboot_UTC": format_utc_display(last_reboot_dt),
        "Last_Reboot_IST": shift_display(last_reboot_dt, 5, 30),
        "Last_Reboot_EDT": shift_display(last_reboot_dt, -4, 0),
        "Reboot_Evidence": reboot_evidence,
        "reboot_status": reboot_status,
        "Previous_Status": previous_status,
        "Status_Change_Signal": status_change_signal,
        "Status_Changed_UTC": status_changed_utc,
        "uniqueKey": f"{customer_id}_{instance_id}",
        "freeformTags": freeform_tags,
        "definedTags": defined_tags,
        "Last_Seen_UTC": format_utc_display(generated_at),
    }
    return row


def build_customers(instances: list[dict[str, Any]], generated_at_epoch_ms: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in instances:
        grouped[row["customerId"]].append(row)

    customers: list[dict[str, Any]] = []
    for customer_id, rows in sorted(grouped.items()):
        customers.append(
            {
                "name": customer_id,
                "lastImport": generated_at_epoch_ms,
                "instanceCount": len(rows),
                "scheduledCount": sum(1 for row in rows if row["reboot_status"] == "Scheduled"),
                "completedCount": sum(1 for row in rows if row["reboot_status"] == "Completed"),
                "changedCount": sum(1 for row in rows if row.get("Status_Change_Signal") not in {"", "-", "Stable"}),
            }
        )
    return customers


def resolve_tenancy_name(identity_client: Any, tenancy_id: str) -> str:
    try:
        return identity_client.get_tenancy(tenancy_id).data.name
    except Exception:
        return "Tenancy"


def process_region(
    args: CollectorArgs,
    config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
    tenancy_id: str,
    tenancy_name: str,
    generated_at: datetime,
    previous_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    emit_event("region_started", region=region, phase="Starting", startedAt=iso_utc(utc_now()))
    log(f"Scanning region {region}")

    emit_event("region_phase", region=region, phase="Collecting Compute", status="running")
    log(f"Collecting compute instances in {region}")
    region_instances, region_warnings = list_instances_for_region(
        config,
        signer,
        region,
        compartments,
        args.compute_timeout_seconds,
    )
    for warning in region_warnings:
        log(warning)

    region_instance_compartments = sorted(
        {
            getattr(instance, "compartment_id", "")
            for instance in region_instances
            if getattr(instance, "compartment_id", "")
        }
    )

    emit_event("region_phase", region=region, phase="Collecting Maintenance Events", status="running")
    log(f"Checking instance maintenance events in {region}")
    try:
        region_maintenance_events, maintenance_warnings = list_maintenance_events_for_region(
            config,
            signer,
            region,
            region_instance_compartments,
            args.maintenance_timeout_seconds,
        )
    except Exception as exc:
        region_maintenance_events = {}
        maintenance_warnings = [f"Instance maintenance event lookup failed in {region}: {exc}"]
    for warning in maintenance_warnings:
        log(warning)
    if region_maintenance_events:
        log(f"Found {len(region_maintenance_events)} active instance maintenance events in {region}")
    else:
        log(f"No active instance maintenance events found in {region}")

    emit_event("region_phase", region=region, phase="Collecting OSMH Boot Data", status="running")
    log(f"Checking OS Management Hub boot data in {region}")
    regional_config = dict(config)
    regional_config["region"] = region
    osmh_client = maybe_build_osmh_client(regional_config, signer)
    if osmh_client is not None:
        log(f"OS Management Hub endpoint for {region}: {osmh_client.base_client.endpoint}")
    try:
        region_last_boots, osmh_timed_out = list_osmh_last_boots_with_timeout(
            config,
            signer,
            region,
            {compartment_id: compartments[compartment_id] for compartment_id in region_instance_compartments if compartment_id in compartments},
            args.osmh_timeout_seconds,
        )
    except Exception as exc:
        region_last_boots = {}
        osmh_timed_out = False
        log(f"OS Management Hub lookup failed in {region}: {exc}")
    if osmh_timed_out:
        log(
            f"OS Management Hub lookup in {region} exceeded {args.osmh_timeout_seconds:g}s; "
            "continuing without boot data for this region"
        )
    elif region_last_boots:
        log(f"Found {len(region_last_boots)} OS Management Hub boot records in {region}")
    else:
        log(f"No OS Management Hub boot records found in {region}")

    emit_event("region_phase", region=region, phase="Building Fleet Rows", status="running")
    rows: list[dict[str, Any]] = []
    for instance in region_instances:
        row = build_instance_row(
            args=args,
            instance=instance,
            region=region,
            tenancy_id=tenancy_id,
            tenancy_name=tenancy_name,
            generated_at=generated_at,
            compartments=compartments,
            maintenance_events=region_maintenance_events,
            last_boots=region_last_boots,
            previous_rows=previous_rows,
        )
        if row is not None:
            rows.append(row)

    log(f"Region {region} contributed {len(rows)} active instances")
    emit_event(
        "region_completed",
        region=region,
        finishedAt=iso_utc(utc_now()),
        instanceCount=len(rows),
    )
    return rows


def main() -> int:
    args = parse_args()
    try:
        config, signer = load_signer_and_config(args)
    except Exception as exc:
        raise SystemExit(
            "Unable to initialize OCI authentication. "
            "If you are running locally, create ~/.oci/config or pass --config-file. "
            "If you are running on OCI compute, try --auth instance_principal.\n"
            f"Details: {exc}"
        ) from exc
    log(f"Starting OCI fleet build with profile '{args.profile if args.auth == 'config' else 'instance_principal'}'")
    tenancy_id = get_tenancy_id(config, signer)
    identity_client = build_identity_client(config, signer)
    tenancy_name = resolve_tenancy_name(identity_client, tenancy_id)
    log(f"Connected to tenancy: {tenancy_name} ({tenancy_id})")
    previous_rows, _ = read_previous_output(args.output)
    compartments = list_compartments(identity_client, tenancy_id)
    log(f"Loaded {len(compartments)} accessible compartments")
    regions = list_regions(identity_client, tenancy_id)
    log(f"Scanning subscribed regions: {', '.join(regions)}")
    emit_event("regions_discovered", regions=regions)

    generated_at = utc_now()
    generated_at_epoch_ms = int(generated_at.timestamp() * 1000)
    instances: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.max_region_workers, max(1, len(regions)))) as executor:
        future_to_region = {
            executor.submit(
                process_region,
                args,
                config,
                signer,
                region,
                compartments,
                tenancy_id,
                tenancy_name,
                generated_at,
                previous_rows,
            ): region
            for region in regions
        }
        for future in concurrent.futures.as_completed(future_to_region):
            region = future_to_region[future]
            try:
                instances.extend(future.result())
            except Exception as exc:
                log(f"Region {region} failed unexpectedly: {exc}")
                emit_event(
                    "region_failed",
                    region=region,
                    phase="Failed",
                    finishedAt=iso_utc(utc_now()),
                )

    instances.sort(key=lambda row: (row["customerId"], row["Region"], row["Display_Name"], row["ID"]))
    payload = {
        "generatedAt": iso_utc(generated_at),
        "generatedAtEpochMs": generated_at_epoch_ms,
        "schemaVersion": 2,
        "source": {
            "type": "oci-sdk",
            "auth": args.auth,
            "profile": args.profile if args.auth == "config" else "instance_principal",
            "customerStrategy": args.customer_strategy,
            "regions": regions,
        },
        "customers": build_customers(instances, generated_at_epoch_ms),
        "instances": instances,
    }

    args.output.write_text(json.dumps(payload, indent=2))
    log(f"Wrote {len(instances)} instances across {len(payload['customers'])} customer groups to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
