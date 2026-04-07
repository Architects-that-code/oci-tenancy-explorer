#!/usr/bin/env python3
"""Serve the OCI portal locally and expose refresh endpoints for OCI data snapshots."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

from build_fleet_data import (
    build_identity_client,
    get_tenancy_id,
    list_call_get_all_results,
    list_compartments,
    list_regions,
    load_signer_and_config,
    resolve_tenancy_name,
)

try:
    import oci
except ImportError:  # pragma: no cover - handled at runtime
    oci = None


ROOT = Path(__file__).resolve().parent
BUILD_SCRIPT = ROOT / "build_fleet_data.py"
OPPORTUNITIES_SCRIPT = ROOT / "build_opportunities_data.py"
SHAPES_SCRIPT = ROOT / "build_shape_data.py"
ANNOUNCEMENTS_SCRIPT = ROOT / "build_announcements_data.py"
FLEET_JSON = ROOT / "fleet_data.json"
OPPORTUNITIES_JSON = ROOT / "fleet_data_opportunities.json"
SHAPES_JSON = ROOT / "fleet_data_shapes.json"
ANNOUNCEMENTS_JSON = ROOT / "fleet_data_announcements.json"
APP_CONFIG_JSON = ROOT / "app_config.json"
UTC = timezone.utc
EVENT_PREFIX = "__OTX_EVENT__ "
SUPPORTED_RESOURCE_TYPES = {"instance", "vnic", "subnet", "vcn", "volume", "bootvolume", "compartment"}
DEFAULT_APP_CONFIG = {
    "experimental-features": {
        "overview": False,
        "shapes": True,
        "opportunities": True,
        "announcements": True,
    }
}


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify_url_prefix(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return normalized or "portal"


def detect_git_branch_slug() -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    branch = (result.stdout or "").strip()
    return slugify_url_prefix(branch) if branch else None


def load_app_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_APP_CONFIG))
    try:
        payload = json.loads(APP_CONFIG_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except Exception:
        return config

    if not isinstance(payload, dict):
        return config

    feature_flags = payload.get("experimental-features")
    if not isinstance(feature_flags, dict):
        feature_flags = payload.get("features")
    if isinstance(feature_flags, dict):
        config["experimental-features"].update({
            key: bool(value)
            for key, value in feature_flags.items()
            if key in config["experimental-features"]
        })
    return config


def feature_enabled(config: dict[str, Any], key: str) -> bool:
    return bool(config.get("experimental-features", {}).get(key, DEFAULT_APP_CONFIG["experimental-features"].get(key, True)))


@dataclass
class RefreshState:
    running: bool = False
    logs: list[str] = field(default_factory=list)
    regions: dict[str, dict[str, Any]] = field(default_factory=dict)
    region_order: list[str] = field(default_factory=list)
    last_exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_success_at: str | None = None
    refresh_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            region_items = [self.regions[name] for name in self.region_order if name in self.regions]
            completed_regions = sum(1 for item in region_items if item.get("status") == "completed")
            failed_regions = sum(1 for item in region_items if item.get("status") == "failed")
            running_regions = sum(1 for item in region_items if item.get("status") == "running")
            total_regions = len(region_items)
            finished_regions = completed_regions + failed_regions
            return {
                "running": self.running,
                "logs": list(self.logs),
                "regions": json.loads(json.dumps(region_items)),
                "lastExitCode": self.last_exit_code,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "lastSuccessAt": self.last_success_at,
                "refreshCount": self.refresh_count,
                "totalRegions": total_regions,
                "completedRegions": completed_regions,
                "finishedRegions": finished_regions,
                "failedRegions": failed_regions,
                "runningRegions": running_regions,
            }

    def append(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-1500:]

    def reset_regions(self) -> None:
        with self.lock:
            self.regions = {}
            self.region_order = []

    def set_regions(self, regions: list[str]) -> None:
        with self.lock:
            self.region_order = list(regions)
            self.regions = {
                region: {
                    "name": region,
                    "status": "queued",
                    "phase": "Queued",
                    "logs": [],
                    "startedAt": None,
                    "finishedAt": None,
                    "instanceCount": 0,
                    "warningCount": 0,
                }
                for region in regions
            }

    def ensure_region(self, region: str) -> None:
        with self.lock:
            if region not in self.regions:
                self.regions[region] = {
                    "name": region,
                    "status": "queued",
                    "phase": "Queued",
                    "logs": [],
                    "startedAt": None,
                    "finishedAt": None,
                    "instanceCount": 0,
                    "warningCount": 0,
                }
                self.region_order.append(region)

    def update_region(self, region: str, **updates: Any) -> None:
        self.ensure_region(region)
        with self.lock:
            self.regions[region].update(updates)

    def append_region_log(self, region: str, message: str) -> None:
        self.ensure_region(region)
        with self.lock:
            region_state = self.regions[region]
            region_state["logs"].append(message)
            region_state["logs"] = region_state["logs"][-250:]
            lowered = message.lower()
            if any(token in lowered for token in ("failed", "timeout", "timed out", "exceeded", "skipping", "error")):
                region_state["warningCount"] = int(region_state.get("warningCount", 0)) + 1


class RefreshRunner:
    def __init__(
        self,
        profile: str,
        config_file: str,
        auth: str,
        script_path: Path,
        output_path: Path,
        label: str,
    ) -> None:
        self.profile = profile
        self.config_file = config_file
        self.auth = auth
        self.script_path = script_path
        self.output_path = output_path
        self.label = label
        self.state = RefreshState()

    def start(self) -> tuple[bool, str]:
        with self.state.lock:
            if self.state.running:
                return False, "Refresh already in progress."
            self.state.running = True
            self.state.logs = []
            self.state.regions = {}
            self.state.region_order = []
            self.state.last_exit_code = None
            self.state.started_at = utc_now_iso()
            self.state.finished_at = None
            self.state.refresh_count += 1

        threading.Thread(target=self._run_refresh, daemon=True).start()
        return True, "Refresh started."

    def _run_refresh(self) -> None:
        command = [
            sys.executable,
            str(self.script_path),
            "--profile",
            self.profile,
            "--config-file",
            self.config_file,
            "--output",
            str(self.output_path),
        ]
        if self.auth != "config":
            command.extend(["--auth", self.auth])

        self.state.append(f"[{utc_now_iso()}] Launching {self.label}")
        self.state.append(f"[{utc_now_iso()}] Command: {' '.join(command)}")

        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                clean_line = line.rstrip()
                if clean_line.startswith(EVENT_PREFIX):
                    self._handle_event_line(clean_line)
                    continue
                self.state.append(clean_line)
                self._route_line_to_region(clean_line)
            exit_code = process.wait()
        except Exception as exc:
            self.state.append(f"Refresh failed to launch: {exc}")
            exit_code = 1

        with self.state.lock:
            self.state.running = False
            self.state.last_exit_code = exit_code
            self.state.finished_at = utc_now_iso()
            if exit_code == 0:
                self.state.last_success_at = self.state.finished_at

        status_line = "Refresh completed successfully." if exit_code == 0 else f"Refresh failed with exit code {exit_code}."
        self.state.append(f"[{utc_now_iso()}] {status_line}")

    def _handle_event_line(self, line: str) -> None:
        try:
            payload = json.loads(line[len(EVENT_PREFIX):])
        except json.JSONDecodeError:
            return

        event_type = payload.get("type")
        if event_type == "regions_discovered":
            self.state.set_regions(payload.get("regions", []))
            return

        region = payload.get("region")
        if not region:
            return

        if event_type == "region_started":
            self.state.update_region(
                region,
                status="running",
                phase=payload.get("phase", "Starting"),
                startedAt=payload.get("startedAt") or utc_now_iso(),
                finishedAt=None,
            )
            return

        if event_type == "region_phase":
            self.state.update_region(
                region,
                status=payload.get("status", "running"),
                phase=payload.get("phase", "Running"),
            )
            return

        if event_type == "region_completed":
            self.state.update_region(
                region,
                status="completed",
                phase="Completed",
                finishedAt=payload.get("finishedAt") or utc_now_iso(),
                instanceCount=int(payload.get("instanceCount", 0) or 0),
            )
            return

        if event_type == "region_failed":
            self.state.update_region(
                region,
                status="failed",
                phase=payload.get("phase", "Failed"),
                finishedAt=payload.get("finishedAt") or utc_now_iso(),
            )

    def _route_line_to_region(self, line: str) -> None:
        with self.state.lock:
            region_names = list(self.state.region_order)
        for name in region_names:
            if name and name in line:
                self.state.append_region_log(name, line)
                break


@dataclass
class SyncStepDefinition:
    key: str
    label: str
    script_path: Path
    output_path: Path


@dataclass
class SyncState:
    running: bool = False
    logs: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    current_step: dict[str, Any] | None = None
    last_exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_success_at: str | None = None
    refresh_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "logs": list(self.logs),
                "steps": json.loads(json.dumps(self.steps)),
                "currentStep": json.loads(json.dumps(self.current_step)) if self.current_step else None,
                "lastExitCode": self.last_exit_code,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "lastSuccessAt": self.last_success_at,
                "refreshCount": self.refresh_count,
            }

    def reset(self, step_definitions: list[SyncStepDefinition]) -> None:
        with self.lock:
            self.logs = []
            self.steps = [
                {
                    "key": step.key,
                    "label": step.label,
                    "status": "queued",
                    "startedAt": None,
                    "finishedAt": None,
                    "lastExitCode": None,
                }
                for step in step_definitions
            ]
            self.current_step = None
            self.last_exit_code = None
            self.started_at = utc_now_iso()
            self.finished_at = None
            self.running = True
            self.refresh_count += 1

    def append(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-2500:]

    def update_step(self, key: str, **updates: Any) -> dict[str, Any] | None:
        with self.lock:
            for step in self.steps:
                if step.get("key") == key:
                    step.update(updates)
                    return dict(step)
        return None

    def set_current_step(self, step_payload: dict[str, Any] | None) -> None:
        with self.lock:
            self.current_step = dict(step_payload) if step_payload else None

    def finish(self, exit_code: int) -> None:
        with self.lock:
            self.running = False
            self.last_exit_code = exit_code
            self.finished_at = utc_now_iso()
            self.current_step = None
            if exit_code == 0:
                self.last_success_at = self.finished_at


class SyncRunner:
    PARTIAL_EXIT_CODE = 2

    def __init__(
        self,
        profile: str,
        config_file: str,
        auth: str,
        step_definitions: list[SyncStepDefinition],
    ) -> None:
        self.profile = profile
        self.config_file = config_file
        self.auth = auth
        self.step_definitions = step_definitions
        self.state = SyncState()

    def start(self, step_definitions: list[SyncStepDefinition] | None = None) -> tuple[bool, str]:
        with self.state.lock:
            if self.state.running:
                return False, "Sync already in progress."
        active_steps = step_definitions or self.step_definitions
        self.state.reset(active_steps)
        threading.Thread(target=self._run_sync, args=(active_steps,), daemon=True).start()
        return True, "Sync started."

    def _build_command(self, step: SyncStepDefinition) -> list[str]:
        command = [
            sys.executable,
            str(step.script_path),
            "--profile",
            self.profile,
            "--config-file",
            self.config_file,
            "--output",
            str(step.output_path),
        ]
        if self.auth != "config":
            command.extend(["--auth", self.auth])
        return command

    def _run_sync(self, step_definitions: list[SyncStepDefinition]) -> None:
        overall_exit_code = 0
        saw_non_fleet_failure = False
        self.state.append(f"[{utc_now_iso()}] Launching unified data sync across {len(step_definitions)} snapshot(s)")

        for index, step in enumerate(step_definitions, start=1):
            started_at = utc_now_iso()
            step_state = self.state.update_step(
                step.key,
                status="running",
                startedAt=started_at,
                finishedAt=None,
                lastExitCode=None,
            )
            self.state.set_current_step(step_state)
            command = self._build_command(step)
            self.state.append(f"[{utc_now_iso()}] Starting step {index}/{len(step_definitions)}: {step.label}")
            self.state.append(f"[{utc_now_iso()}] Command ({step.label}): {' '.join(command)}")

            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    clean_line = line.rstrip()
                    self.state.append(f"[{step.label}] {clean_line}")
                exit_code = process.wait()
            except Exception as exc:
                self.state.append(f"[{step.label}] Refresh failed to launch: {exc}")
                exit_code = 1

            finished_at = utc_now_iso()
            step_state = self.state.update_step(
                step.key,
                status="completed" if exit_code == 0 else "failed",
                finishedAt=finished_at,
                lastExitCode=exit_code,
            )
            self.state.set_current_step(step_state)

            if exit_code == 0:
                self.state.append(f"[{utc_now_iso()}] Step completed successfully: {step.label}")
                continue

            if step.key == "fleet":
                overall_exit_code = 1
                self.state.append(f"[{utc_now_iso()}] Fleet failed, so sync is stopping before later steps run.")
                break

            saw_non_fleet_failure = True
            overall_exit_code = self.PARTIAL_EXIT_CODE
            self.state.append(f"[{utc_now_iso()}] Step failed but sync will continue: {step.label}")

        if overall_exit_code == 0 and saw_non_fleet_failure:
            overall_exit_code = self.PARTIAL_EXIT_CODE

        if overall_exit_code == 0:
            self.state.append(f"[{utc_now_iso()}] Unified sync completed successfully.")
        elif overall_exit_code == self.PARTIAL_EXIT_CODE:
            self.state.append(f"[{utc_now_iso()}] Unified sync completed with partial success.")
        else:
            self.state.append(f"[{utc_now_iso()}] Unified sync failed before completion.")

        self.state.finish(overall_exit_code)


class ResourceLookupService:
    def __init__(self, profile: str, config_file: str, auth: str) -> None:
        self.profile = profile
        self.config_file = config_file
        self.auth = auth
        self._lock = threading.Lock()
        self._context: dict[str, Any] | None = None

    def _load_context(self) -> dict[str, Any]:
        with self._lock:
            if self._context is not None:
                return self._context

            if oci is None:
                raise RuntimeError("The OCI Python SDK is not installed on the portal host.")
            args = SimpleNamespace(auth=self.auth, config_file=self.config_file, profile=self.profile)
            config, signer = load_signer_and_config(args)
            identity_client = build_identity_client(config, signer)
            tenancy_id = get_tenancy_id(config, signer)
            self._context = {
                "config": config,
                "signer": signer,
                "identity_client": identity_client,
                "tenancy_id": tenancy_id,
                "tenancy_name": resolve_tenancy_name(identity_client, tenancy_id),
                "regions": list_regions(identity_client, tenancy_id),
                "compartments": list_compartments(identity_client, tenancy_id),
            }
            return self._context

    def lookup(self, ocid: str) -> dict[str, Any]:
        normalized_ocid = str(ocid or "").strip()
        if not normalized_ocid:
            return self._response(
                ok=False,
                found=False,
                supported=False,
                ocid=normalized_ocid,
                message="Provide an OCID to inspect.",
            )
        if not self._looks_like_ocid(normalized_ocid):
            return self._response(
                ok=False,
                found=False,
                supported=False,
                ocid=normalized_ocid,
                message="That value does not look like a valid OCI OCID.",
            )

        resource_type = self._resource_type_from_ocid(normalized_ocid)
        if resource_type not in SUPPORTED_RESOURCE_TYPES:
            return self._response(
                ok=True,
                found=False,
                supported=False,
                ocid=normalized_ocid,
                resource_type=resource_type,
                message=f"Lookup is not yet supported for OCID type '{resource_type or 'unknown'}'.",
            )

        try:
            context = self._load_context()
        except Exception as exc:
            return self._response(
                ok=False,
                found=False,
                supported=True,
                ocid=normalized_ocid,
                resource_type=resource_type,
                message=f"Unable to initialize OCI lookup context: {exc}",
            )

        try:
            if resource_type == "compartment":
                return self._lookup_compartment(context, normalized_ocid)
            return self._lookup_regional_resource(context, resource_type, normalized_ocid)
        except Exception as exc:
            return self._response(
                ok=False,
                found=False,
                supported=True,
                ocid=normalized_ocid,
                resource_type=resource_type,
                message=f"Resource lookup failed unexpectedly: {exc}",
            )

    def _lookup_regional_resource(self, context: dict[str, Any], resource_type: str, ocid: str) -> dict[str, Any]:
        warnings: list[str] = []
        for region in context["regions"]:
            try:
                if resource_type == "instance":
                    return self._lookup_instance(context, region, ocid)
                if resource_type == "vnic":
                    return self._lookup_vnic(context, region, ocid)
                if resource_type == "subnet":
                    return self._lookup_subnet(context, region, ocid)
                if resource_type == "vcn":
                    return self._lookup_vcn(context, region, ocid)
                if resource_type == "volume":
                    return self._lookup_volume(context, region, ocid)
                if resource_type == "bootvolume":
                    return self._lookup_boot_volume(context, region, ocid)
            except Exception as exc:
                if self._is_retryable_lookup_error(exc):
                    warnings.append(f"{region}: {self._service_error_text(exc)}")
                    continue
                return self._response(
                    ok=False,
                    found=False,
                    supported=True,
                    ocid=ocid,
                    resource_type=resource_type,
                    message=f"Lookup failed in {region}: {self._service_error_text(exc)}",
                    warnings=warnings,
                )

        message = f"No accessible {resource_type} resource was found for that OCID in the subscribed regions."
        if warnings:
            message = f"{message} Lookup warnings were recorded while checking regions."
        return self._response(
            ok=True,
            found=False,
            supported=True,
            ocid=ocid,
            resource_type=resource_type,
            message=message,
            warnings=warnings,
        )

    def _lookup_compartment(self, context: dict[str, Any], ocid: str) -> dict[str, Any]:
        identity_client = context["identity_client"]
        compartment = identity_client.get_compartment(ocid).data
        parent_name = context["compartments"].get(getattr(compartment, "compartment_id", ""), {}).get("name", "")
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="compartment",
            display_name=getattr(compartment, "name", "") or ocid,
            region="",
            compartment={"id": getattr(compartment, "compartment_id", "") or "", "name": parent_name},
            lifecycle_state=getattr(compartment, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Name", getattr(compartment, "name", "") or ""),
                self._field("Description", getattr(compartment, "description", "") or ""),
                self._field("State", getattr(compartment, "lifecycle_state", "") or ""),
                self._field("Parent Compartment", parent_name or "-"),
                self._field("Parent Compartment OCID", getattr(compartment, "compartment_id", "") or "", ocid=getattr(compartment, "compartment_id", "") or ""),
                self._field("Time Created", self._string_value(getattr(compartment, "time_created", None))),
            ],
            related_links=self._related_links(
                ("Parent Compartment", getattr(compartment, "compartment_id", "") or ""),
            ),
            console_url=self._console_url(None),
        )

    def _lookup_instance(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        compute_client = self._build_client(oci.core.ComputeClient, context["config"], context["signer"], region)
        network_client = self._build_client(oci.core.VirtualNetworkClient, context["config"], context["signer"], region)
        instance = compute_client.get_instance(ocid).data
        attachments_response = list_call_get_all_results(
            compute_client.list_vnic_attachments,
            compartment_id=getattr(instance, "compartment_id", ""),
            instance_id=ocid,
        )
        attachments = [] if attachments_response is None or getattr(attachments_response, "data", None) is None else attachments_response.data
        vnic_rows: list[dict[str, Any]] = []
        for attachment in attachments:
            vnic_id = getattr(attachment, "vnic_id", "") or ""
            if not vnic_id:
                continue
            try:
                vnic = network_client.get_vnic(vnic_id).data
            except Exception:
                continue
            vnic_rows.append(
                {
                    "id": vnic_id,
                    "name": getattr(vnic, "display_name", "") or "",
                    "private_ip": getattr(vnic, "private_ip", "") or "",
                    "public_ip": getattr(vnic, "public_ip", "") or "",
                    "subnet_id": getattr(vnic, "subnet_id", "") or "",
                    "vcn_id": getattr(vnic, "vcn_id", "") or "",
                    "nsg_ids": list(getattr(vnic, "nsg_ids", []) or []),
                    "is_primary": bool(getattr(attachment, "is_primary", False)),
                }
            )
        vnic_rows.sort(key=lambda item: (0 if item["is_primary"] else 1, item["id"]))
        primary_vnic = vnic_rows[0] if vnic_rows else None
        subnet = self._safe_get(network_client.get_subnet, primary_vnic["subnet_id"]) if primary_vnic and primary_vnic.get("subnet_id") else None
        vcn = self._safe_get(network_client.get_vcn, primary_vnic["vcn_id"]) if primary_vnic and primary_vnic.get("vcn_id") else None
        compartment_id = getattr(instance, "compartment_id", "") or ""
        compartment_name = context["compartments"].get(compartment_id, {}).get("name", "")
        public_ip_count = sum(1 for row in vnic_rows if row.get("public_ip"))
        summary_fields = [
            self._field("OCID", ocid, ocid=ocid),
            self._field("Display Name", getattr(instance, "display_name", "") or ""),
            self._field("State", getattr(instance, "lifecycle_state", "") or ""),
            self._field("Region", region),
            self._field("Compartment", compartment_name or "-"),
            self._field("Compartment OCID", compartment_id, ocid=compartment_id),
            self._field("Shape", getattr(instance, "shape", "") or ""),
            self._field("Availability Domain", getattr(instance, "availability_domain", "") or ""),
            self._field("Fault Domain", getattr(instance, "fault_domain", "") or ""),
            self._field("Primary Private IP", primary_vnic["private_ip"] if primary_vnic else ""),
            self._field("Primary Public IP", primary_vnic["public_ip"] if primary_vnic else ""),
            self._field("Primary VNIC", primary_vnic["name"] if primary_vnic else "-"),
            self._field("Primary VNIC OCID", primary_vnic["id"] if primary_vnic else "", ocid=primary_vnic["id"] if primary_vnic else ""),
            self._field("Subnet", getattr(subnet, "display_name", "") if subnet is not None else ""),
            self._field("Subnet OCID", getattr(subnet, "id", "") if subnet is not None else "", ocid=getattr(subnet, "id", "") if subnet is not None else ""),
            self._field("VCN", getattr(vcn, "display_name", "") if vcn is not None else ""),
            self._field("VCN OCID", getattr(vcn, "id", "") if vcn is not None else "", ocid=getattr(vcn, "id", "") if vcn is not None else ""),
            self._field("VNIC Count", len(vnic_rows)),
            self._field("Public IP Count", public_ip_count),
            self._field("Time Created", self._string_value(getattr(instance, "time_created", None))),
        ]
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="instance",
            display_name=getattr(instance, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": compartment_name},
            lifecycle_state=getattr(instance, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=summary_fields,
            related_links=self._related_links(
                ("Compartment", compartment_id),
                ("Primary VNIC", primary_vnic["id"] if primary_vnic else ""),
                ("Subnet", getattr(subnet, "id", "") if subnet is not None else ""),
                ("VCN", getattr(vcn, "id", "") if vcn is not None else ""),
            ),
            console_url=self._console_url(region),
        )

    def _lookup_vnic(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        network_client = self._build_client(oci.core.VirtualNetworkClient, context["config"], context["signer"], region)
        vnic = network_client.get_vnic(ocid).data
        subnet = self._safe_get(network_client.get_subnet, getattr(vnic, "subnet_id", "") or "")
        vcn = self._safe_get(network_client.get_vcn, getattr(vnic, "vcn_id", "") or "")
        compartment_id = getattr(vnic, "compartment_id", "") or ""
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="vnic",
            display_name=getattr(vnic, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": context["compartments"].get(compartment_id, {}).get("name", "")},
            lifecycle_state=getattr(vnic, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Display Name", getattr(vnic, "display_name", "") or ""),
                self._field("Region", region),
                self._field("Compartment OCID", compartment_id, ocid=compartment_id),
                self._field("Lifecycle State", getattr(vnic, "lifecycle_state", "") or ""),
                self._field("Private IP", getattr(vnic, "private_ip", "") or ""),
                self._field("Public IP", getattr(vnic, "public_ip", "") or ""),
                self._field("Subnet", getattr(subnet, "display_name", "") if subnet is not None else ""),
                self._field("Subnet OCID", getattr(vnic, "subnet_id", "") or "", ocid=getattr(vnic, "subnet_id", "") or ""),
                self._field("VCN", getattr(vcn, "display_name", "") if vcn is not None else ""),
                self._field("VCN OCID", getattr(vnic, "vcn_id", "") or "", ocid=getattr(vnic, "vcn_id", "") or ""),
                self._field("NSG Count", len(list(getattr(vnic, "nsg_ids", []) or []))),
                self._field("NSG OCIDs", "\n".join(list(getattr(vnic, "nsg_ids", []) or []))),
            ],
            related_links=self._related_links(
                ("Compartment", compartment_id),
                ("Subnet", getattr(vnic, "subnet_id", "") or ""),
                ("VCN", getattr(vnic, "vcn_id", "") or ""),
                *[(f"NSG {index + 1}", nsg_id) for index, nsg_id in enumerate(list(getattr(vnic, "nsg_ids", []) or []))]
            ),
            console_url=self._console_url(region),
        )

    def _lookup_subnet(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        network_client = self._build_client(oci.core.VirtualNetworkClient, context["config"], context["signer"], region)
        subnet = network_client.get_subnet(ocid).data
        compartment_id = getattr(subnet, "compartment_id", "") or ""
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="subnet",
            display_name=getattr(subnet, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": context["compartments"].get(compartment_id, {}).get("name", "")},
            lifecycle_state=getattr(subnet, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Display Name", getattr(subnet, "display_name", "") or ""),
                self._field("Region", region),
                self._field("Compartment OCID", compartment_id, ocid=compartment_id),
                self._field("Lifecycle State", getattr(subnet, "lifecycle_state", "") or ""),
                self._field("CIDR Block", getattr(subnet, "cidr_block", "") or ""),
                self._field("Availability Domain", getattr(subnet, "availability_domain", "") or ""),
                self._field("VCN OCID", getattr(subnet, "vcn_id", "") or "", ocid=getattr(subnet, "vcn_id", "") or ""),
                self._field("Route Table OCID", getattr(subnet, "route_table_id", "") or "", ocid=getattr(subnet, "route_table_id", "") or ""),
                self._field("Security List OCIDs", "\n".join(list(getattr(subnet, "security_list_ids", []) or []))),
                self._field("Public IPs Allowed", "No" if getattr(subnet, "prohibit_public_ip_on_vnic", False) else "Yes"),
            ],
            related_links=self._related_links(
                ("Compartment", compartment_id),
                ("VCN", getattr(subnet, "vcn_id", "") or ""),
                ("Route Table", getattr(subnet, "route_table_id", "") or ""),
                *[(f"Security List {index + 1}", security_list_id) for index, security_list_id in enumerate(list(getattr(subnet, "security_list_ids", []) or []))]
            ),
            console_url=self._console_url(region),
        )

    def _lookup_vcn(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        network_client = self._build_client(oci.core.VirtualNetworkClient, context["config"], context["signer"], region)
        vcn = network_client.get_vcn(ocid).data
        compartment_id = getattr(vcn, "compartment_id", "") or ""
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="vcn",
            display_name=getattr(vcn, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": context["compartments"].get(compartment_id, {}).get("name", "")},
            lifecycle_state=getattr(vcn, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Display Name", getattr(vcn, "display_name", "") or ""),
                self._field("Region", region),
                self._field("Compartment OCID", compartment_id, ocid=compartment_id),
                self._field("Lifecycle State", getattr(vcn, "lifecycle_state", "") or ""),
                self._field("CIDR Blocks", "\n".join(list(getattr(vcn, "cidr_blocks", []) or [])) or getattr(vcn, "cidr_block", "") or ""),
                self._field("DNS Label", getattr(vcn, "dns_label", "") or ""),
            ],
            related_links=self._related_links(
                ("Compartment", compartment_id),
            ),
            console_url=self._console_url(region),
        )

    def _lookup_volume(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        block_client = self._build_client(oci.core.BlockstorageClient, context["config"], context["signer"], region)
        volume = block_client.get_volume(ocid).data
        compartment_id = getattr(volume, "compartment_id", "") or ""
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="volume",
            display_name=getattr(volume, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": context["compartments"].get(compartment_id, {}).get("name", "")},
            lifecycle_state=getattr(volume, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Display Name", getattr(volume, "display_name", "") or ""),
                self._field("Region", region),
                self._field("Compartment OCID", compartment_id, ocid=compartment_id),
                self._field("Lifecycle State", getattr(volume, "lifecycle_state", "") or ""),
                self._field("Size (GB)", getattr(volume, "size_in_gbs", "") or ""),
                self._field("Availability Domain", getattr(volume, "availability_domain", "") or ""),
                self._field("Volume Group OCID", getattr(volume, "volume_group_id", "") or "", ocid=getattr(volume, "volume_group_id", "") or ""),
                self._field("Time Created", self._string_value(getattr(volume, "time_created", None))),
            ],
            related_links=self._related_links(
                ("Compartment", compartment_id),
                ("Volume Group", getattr(volume, "volume_group_id", "") or ""),
            ),
            console_url=self._console_url(region),
        )

    def _lookup_boot_volume(self, context: dict[str, Any], region: str, ocid: str) -> dict[str, Any]:
        block_client = self._build_client(oci.core.BlockstorageClient, context["config"], context["signer"], region)
        boot_volume = block_client.get_boot_volume(ocid).data
        compartment_id = getattr(boot_volume, "compartment_id", "") or ""
        return self._response(
            ok=True,
            found=True,
            supported=True,
            ocid=ocid,
            resource_type="bootvolume",
            display_name=getattr(boot_volume, "display_name", "") or ocid,
            region=region,
            compartment={"id": compartment_id, "name": context["compartments"].get(compartment_id, {}).get("name", "")},
            lifecycle_state=getattr(boot_volume, "lifecycle_state", "") or "",
            source={
                "type": "live_oci_api",
                "auth": self.auth,
                "profile": self.profile if self.auth == "config" else "instance_principal",
                "tenancy": context["tenancy_name"],
            },
            summary_fields=[
                self._field("OCID", ocid, ocid=ocid),
                self._field("Display Name", getattr(boot_volume, "display_name", "") or ""),
                self._field("Region", region),
                self._field("Compartment OCID", compartment_id, ocid=compartment_id),
                self._field("Lifecycle State", getattr(boot_volume, "lifecycle_state", "") or ""),
                self._field("Size (GB)", getattr(boot_volume, "size_in_gbs", "") or ""),
                self._field("Availability Domain", getattr(boot_volume, "availability_domain", "") or ""),
                self._field("KMS Key OCID", getattr(boot_volume, "kms_key_id", "") or "", ocid=getattr(boot_volume, "kms_key_id", "") or ""),
                self._field("Time Created", self._string_value(getattr(boot_volume, "time_created", None))),
            ],
            related_links=self._related_links(
                ("Compartment", compartment_id),
                ("KMS Key", getattr(boot_volume, "kms_key_id", "") or ""),
            ),
            console_url=self._console_url(region),
        )

    def _build_client(self, client_cls: Any, config: dict[str, Any], signer: Any, region: str | None):
        regional_config = dict(config)
        if region:
            regional_config["region"] = region
        kwargs = {"config": regional_config}
        if signer is not None:
            kwargs["signer"] = signer
        return client_cls(**kwargs)

    def _field(self, label: str, value: Any, *, ocid: str = "") -> dict[str, Any]:
        return {
            "label": label,
            "value": self._string_value(value),
            "copyValue": self._string_value(value),
            "ocid": ocid or "",
        }

    def _related_links(self, *items: tuple[str, str]) -> list[dict[str, str]]:
        links = []
        seen: set[tuple[str, str]] = set()
        for label, ocid in items:
            if not ocid:
                continue
            key = (label, ocid)
            if key in seen:
                continue
            seen.add(key)
            links.append({"label": label, "ocid": ocid})
        return links

    def _response(self, **payload: Any) -> dict[str, Any]:
        payload.setdefault("summary_fields", [])
        payload.setdefault("related_links", [])
        payload.setdefault("warnings", [])
        return payload

    def _safe_get(self, getter: Any, ocid: str) -> Any | None:
        try:
            return getter(ocid).data
        except Exception:
            return None

    def _console_url(self, region: str | None) -> str:
        if region:
            return f"https://cloud.oracle.com/?region={region}"
        return "https://cloud.oracle.com/"

    def _string_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat()
        return str(value)

    def _looks_like_ocid(self, value: str) -> bool:
        parts = value.split(".")
        return len(parts) >= 5 and parts[0].lower() == "ocid1" and bool(parts[1])

    def _resource_type_from_ocid(self, ocid: str) -> str:
        parts = ocid.split(".")
        return parts[1].lower() if len(parts) > 1 else ""

    def _is_retryable_lookup_error(self, exc: Exception) -> bool:
        if oci is None:
            return False
        service_error = getattr(oci.exceptions, "ServiceError", None)
        if service_error is not None and isinstance(exc, service_error):
            return int(getattr(exc, "status", 0) or 0) in {0, 401, 403, 404, 409}
        return False

    def _service_error_text(self, exc: Exception) -> str:
        if oci is not None:
            service_error = getattr(oci.exceptions, "ServiceError", None)
            if service_error is not None and isinstance(exc, service_error):
                code = getattr(exc, "code", "") or "ServiceError"
                message = getattr(exc, "message", "") or str(exc)
                return f"{code}: {message}"
        return str(exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the OCI Tenancy Explorer locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--config-file", default=str(Path("~/.oci/config").expanduser()))
    parser.add_argument("--auth", choices=["config", "instance_principal"], default="config")
    parser.add_argument("--url-prefix", default="")
    return parser.parse_args()


def make_handler(
    fleet_runner: RefreshRunner,
    opportunities_runner: RefreshRunner,
    shapes_runner: RefreshRunner,
    announcements_runner: RefreshRunner,
    sync_runner: SyncRunner,
    resource_lookup_service: ResourceLookupService,
    url_prefix: str,
):
    def current_app_config() -> dict[str, Any]:
        return load_app_config()

    def enabled_sync_steps() -> list[SyncStepDefinition]:
        config = current_app_config()
        filtered_steps: list[SyncStepDefinition] = []
        for step in sync_runner.step_definitions:
            if step.key == "shapes" and not feature_enabled(config, "shapes"):
                continue
            if step.key == "opportunities" and not feature_enabled(config, "opportunities"):
                continue
            if step.key == "announcements" and not feature_enabled(config, "announcements"):
                continue
            filtered_steps.append(step)
        return filtered_steps

    class PortalHandler(SimpleHTTPRequestHandler):
        normalized_prefix = f"/{url_prefix}" if url_prefix else ""

        def _any_single_refresh_running(self) -> bool:
            return any(
                runner.state.snapshot().get("running")
                for runner in (fleet_runner, opportunities_runner, shapes_runner, announcements_runner)
            )

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def _normalize_request_path(self) -> tuple[str, str]:
            parsed = urlparse(self.path)
            path = parsed.path or "/"

            if self.normalized_prefix and path == self.normalized_prefix:
                path = f"{self.normalized_prefix}/"

            if self.normalized_prefix and path.startswith(f"{self.normalized_prefix}/"):
                stripped = path[len(self.normalized_prefix):] or "/"
                normalized = stripped if stripped.startswith("/") else f"/{stripped}"
                return normalized, parsed.query or ""

            return path, parsed.query or ""

        def _set_normalized_path(self, path: str, query: str) -> None:
            self.path = f"{path}?{query}" if query else path

        def _redirect_to_prefixed_index(self) -> None:
            target = f"{self.normalized_prefix}/index.html"
            self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
            self.send_header("Location", target)
            self.end_headers()

        def _feature_disabled_response(self, label: str) -> None:
            self._send_json(
                {
                    "started": False,
                    "message": f"{label} is currently disabled by app_config.json.",
                    "logs": [f"{label} is currently disabled by app_config.json."],
                    "lastExitCode": 1,
                },
                status=HTTPStatus.CONFLICT,
            )

        def do_GET(self) -> None:
            normalized_path, query_string = self._normalize_request_path()
            query = parse_qs(query_string)
            if self.normalized_prefix and normalized_path in {"/", "/index.html"} and self.path in {"/", "/index.html"}:
                self._redirect_to_prefixed_index()
                return
            if normalized_path == "/api/refresh/status":
                self._send_json(fleet_runner.state.snapshot())
                return
            if normalized_path == "/api/opportunities/status":
                self._send_json(opportunities_runner.state.snapshot())
                return
            if normalized_path == "/api/shapes/status":
                self._send_json(shapes_runner.state.snapshot())
                return
            if normalized_path == "/api/announcements/status":
                self._send_json(announcements_runner.state.snapshot())
                return
            if normalized_path == "/api/sync/status":
                self._send_json(sync_runner.state.snapshot())
                return
            if normalized_path == "/api/resource/lookup":
                ocid = (query.get("ocid") or [""])[0]
                self._send_json(resource_lookup_service.lookup(ocid))
                return
            if normalized_path == "/api/export/bundle":
                customer_name = (query.get("name") or [""])[0].strip()
                if not customer_name:
                    self._send_json({"error": "Customer name is required. Provide ?name=YourName"}, status=HTTPStatus.BAD_REQUEST)
                    return
                bundle: dict[str, Any] = {"bundleVersion": 1, "exportedAt": utc_now_iso(), "customerName": customer_name}
                for label, path in [
                    ("fleet_data", FLEET_JSON),
                    ("fleet_data_shapes", SHAPES_JSON),
                    ("fleet_data_opportunities", OPPORTUNITIES_JSON),
                    ("fleet_data_announcements", ANNOUNCEMENTS_JSON),
                ]:
                    if path.exists():
                        try:
                            bundle[label] = json.loads(path.read_text(encoding="utf-8"))
                        except Exception:
                            bundle[label] = None
                    else:
                        bundle[label] = None
                if not any(bundle.get(k) for k in ("fleet_data", "fleet_data_shapes", "fleet_data_opportunities", "fleet_data_announcements")):
                    self._send_json({"error": "No snapshot files found. Run Sync Data first."}, status=HTTPStatus.NOT_FOUND)
                    return
                body = json.dumps(bundle).encode("utf-8")
                safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in customer_name)
                filename = f"{safe_name}_bundle.json"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self._set_normalized_path(normalized_path, query_string)
            super().do_GET()

        def do_POST(self) -> None:
            normalized_path, _query_string = self._normalize_request_path()
            if normalized_path == "/api/refresh":
                if sync_runner.state.snapshot().get("running"):
                    self._send_json({"started": False, "message": "Unified sync is already in progress.", **fleet_runner.state.snapshot()}, status=HTTPStatus.CONFLICT)
                    return
                started, message = fleet_runner.start()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **fleet_runner.state.snapshot()}, status=status)
                return
            if normalized_path == "/api/opportunities/refresh":
                if not feature_enabled(current_app_config(), "opportunities"):
                    self._feature_disabled_response("Opportunities")
                    return
                if sync_runner.state.snapshot().get("running"):
                    self._send_json({"started": False, "message": "Unified sync is already in progress.", **opportunities_runner.state.snapshot()}, status=HTTPStatus.CONFLICT)
                    return
                started, message = opportunities_runner.start()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **opportunities_runner.state.snapshot()}, status=status)
                return
            if normalized_path == "/api/shapes/refresh":
                if not feature_enabled(current_app_config(), "shapes"):
                    self._feature_disabled_response("Shape Explorer")
                    return
                if sync_runner.state.snapshot().get("running"):
                    self._send_json({"started": False, "message": "Unified sync is already in progress.", **shapes_runner.state.snapshot()}, status=HTTPStatus.CONFLICT)
                    return
                started, message = shapes_runner.start()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **shapes_runner.state.snapshot()}, status=status)
                return
            if normalized_path == "/api/announcements/refresh":
                if not feature_enabled(current_app_config(), "announcements"):
                    self._feature_disabled_response("Console Announcements")
                    return
                if sync_runner.state.snapshot().get("running"):
                    self._send_json({"started": False, "message": "Unified sync is already in progress.", **announcements_runner.state.snapshot()}, status=HTTPStatus.CONFLICT)
                    return
                started, message = announcements_runner.start()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **announcements_runner.state.snapshot()}, status=status)
                return
            if normalized_path == "/api/sync/refresh":
                if self._any_single_refresh_running() or sync_runner.state.snapshot().get("running"):
                    snapshot = sync_runner.state.snapshot()
                    self._send_json({"started": False, "message": "Another refresh is already in progress.", **snapshot}, status=HTTPStatus.CONFLICT)
                    return
                started, message = sync_runner.start(enabled_sync_steps())
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **sync_runner.state.snapshot()}, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[portal-server] {self.address_string()} - {fmt % args}")

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return PortalHandler


def main() -> int:
    args = parse_args()
    url_prefix = slugify_url_prefix(args.url_prefix) if args.url_prefix else (detect_git_branch_slug() or slugify_url_prefix(ROOT.name))
    fleet_runner = RefreshRunner(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
        script_path=BUILD_SCRIPT,
        output_path=FLEET_JSON,
        label="OCI fleet collector",
    )
    opportunities_runner = RefreshRunner(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
        script_path=OPPORTUNITIES_SCRIPT,
        output_path=OPPORTUNITIES_JSON,
        label="OCI opportunities collector",
    )
    shapes_runner = RefreshRunner(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
        script_path=SHAPES_SCRIPT,
        output_path=SHAPES_JSON,
        label="OCI shape collector",
    )
    announcements_runner = RefreshRunner(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
        script_path=ANNOUNCEMENTS_SCRIPT,
        output_path=ANNOUNCEMENTS_JSON,
        label="OCI announcements collector",
    )
    sync_runner = SyncRunner(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
        step_definitions=[
            SyncStepDefinition("fleet", "Fleet", BUILD_SCRIPT, FLEET_JSON),
            SyncStepDefinition("shapes", "Shape Explorer", SHAPES_SCRIPT, SHAPES_JSON),
            SyncStepDefinition("opportunities", "Opportunities", OPPORTUNITIES_SCRIPT, OPPORTUNITIES_JSON),
            SyncStepDefinition("announcements", "Console Announcements", ANNOUNCEMENTS_SCRIPT, ANNOUNCEMENTS_JSON),
        ],
    )
    resource_lookup_service = ResourceLookupService(
        profile=args.profile,
        config_file=args.config_file,
        auth=args.auth,
    )
    handler = make_handler(
        fleet_runner,
        opportunities_runner,
        shapes_runner,
        announcements_runner,
        sync_runner,
        resource_lookup_service,
        url_prefix,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/{url_prefix}/index.html"
    fallback_url = f"http://{args.host}:{args.port}/index.html"
    print(f"Serving OCI Tenancy Explorer at {url}")
    print(f"Fallback root URL remains available at {fallback_url}")
    print(f"Refresh endpoints use profile '{args.profile}' and config '{args.config_file}'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down portal server...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())