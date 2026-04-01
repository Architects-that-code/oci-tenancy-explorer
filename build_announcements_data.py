#!/usr/bin/env python3
"""
Build fleet_data_announcements.json from Oracle Cloud Infrastructure.

This collector snapshots OCI Console Announcements so the Tenancy Explorer can
surface them in a customer-friendly tab without relying on ad hoc console
screenshots or manual weekly handoffs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import oci

from build_fleet_data import (
    build_identity_client,
    emit_event,
    get_tenancy_id,
    iso_utc,
    list_call_get_all_results,
    load_signer_and_config,
    log,
    require_oci_sdk,
    resolve_tenancy_name,
    utc_now,
)


DEFAULT_OUTPUT = "fleet_data_announcements.json"


@dataclass
class AnnouncementsArgs:
    auth: str
    config_file: str
    profile: str
    output: Path
    max_detail_workers: int


def parse_args() -> AnnouncementsArgs:
    parser = argparse.ArgumentParser(description="Build OCI console announcements snapshot from OCI APIs.")
    parser.add_argument("--auth", choices=["config", "instance_principal"], default="config")
    parser.add_argument("--config-file", default="~/.oci/config")
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--max-detail-workers",
        type=int,
        default=4,
        help="Maximum number of concurrent announcement detail lookups.",
    )
    ns = parser.parse_args()
    return AnnouncementsArgs(
        auth=ns.auth,
        config_file=ns.config_file,
        profile=ns.profile,
        output=Path(ns.output).expanduser(),
        max_detail_workers=max(1, ns.max_detail_workers),
    )


def build_announcement_client(config: dict[str, Any], signer: Any):
    kwargs = {"config": config}
    if signer is not None:
        kwargs["signer"] = signer
    return oci.announcements_service.AnnouncementClient(**kwargs)


def normalize_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, (list, tuple, set)):
        return [str(value) for value in values if value]
    return [str(values)]


def normalize_resource(resource: Any) -> dict[str, str]:
    return {
        "resourceId": getattr(resource, "resource_id", "") or "",
        "resourceName": getattr(resource, "resource_name", "") or "",
        "region": getattr(resource, "region", "") or "",
    }


def normalize_announcement(summary: Any, detail: Any | None) -> dict[str, Any]:
    source = detail or summary
    affected_resources = [
        normalize_resource(resource)
        for resource in (getattr(detail, "affected_resources", None) or [])
    ]
    return {
        "id": getattr(summary, "id", "") or "",
        "summary": getattr(summary, "summary", "") or "",
        "description": getattr(source, "description", "") or "",
        "additionalInformation": getattr(source, "additional_information", "") or "",
        "announcementType": getattr(summary, "announcement_type", "") or "",
        "lifecycleState": getattr(summary, "lifecycle_state", "") or "",
        "isBanner": bool(getattr(summary, "is_banner", False)),
        "services": normalize_list(getattr(summary, "services", None)),
        "affectedRegions": normalize_list(getattr(summary, "affected_regions", None)),
        "referenceTicketNumber": getattr(summary, "reference_ticket_number", "") or "",
        "environmentName": getattr(summary, "environment_name", "") or "",
        "platformType": getattr(summary, "platform_type", "") or "",
        "chainId": getattr(summary, "chain_id", "") or "",
        "timeCreated": iso_utc(getattr(summary, "time_created", None)),
        "timeUpdated": iso_utc(getattr(summary, "time_updated", None)),
        "timeOne": {
            "title": getattr(summary, "time_one_title", "") or "",
            "type": getattr(summary, "time_one_type", "") or "",
            "value": iso_utc(getattr(summary, "time_one_value", None)),
        },
        "timeTwo": {
            "title": getattr(summary, "time_two_title", "") or "",
            "type": getattr(summary, "time_two_type", "") or "",
            "value": iso_utc(getattr(summary, "time_two_value", None)),
        },
        "affectedResources": affected_resources,
        "affectedResourceCount": len(affected_resources),
        "detailSource": "detail" if detail is not None else "summary",
    }


def fetch_announcement_detail(client: Any, announcement_id: str) -> tuple[str, Any | None, str | None]:
    try:
        detail = client.get_announcement(announcement_id).data
        return announcement_id, detail, None
    except Exception as exc:  # pragma: no cover - detail access can vary by tenancy/IAM
        return announcement_id, None, f"Announcement detail lookup failed for {announcement_id}: {exc}"


def build_summary(announcements: list[dict[str, Any]]) -> dict[str, Any]:
    type_counter = Counter((item.get("announcementType") or "UNKNOWN") for item in announcements)
    service_counter = Counter()
    region_counter = Counter()
    for item in announcements:
        service_counter.update(item.get("services") or [])
        region_counter.update(item.get("affectedRegions") or [])

    return {
        "announcementCount": len(announcements),
        "activeCount": sum(1 for item in announcements if item.get("lifecycleState") == "ACTIVE"),
        "inactiveCount": sum(1 for item in announcements if item.get("lifecycleState") == "INACTIVE"),
        "bannerCount": sum(1 for item in announcements if item.get("isBanner")),
        "serviceCount": len(service_counter),
        "regionCount": len(region_counter),
        "actionRequiredCount": int(type_counter.get("ACTION_REQUIRED", 0)),
        "actionRecommendedCount": int(type_counter.get("ACTION_RECOMMENDED", 0)),
        "topServices": [
            {"service": name, "announcementCount": count}
            for name, count in service_counter.most_common(8)
        ],
        "typeBreakdown": [
            {"announcementType": name, "announcementCount": count}
            for name, count in sorted(type_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def main() -> int:
    require_oci_sdk()
    args = parse_args()
    config, signer = load_signer_and_config(args)
    generated_at = utc_now()

    tenancy_id = get_tenancy_id(config, signer)
    tenancy_name = resolve_tenancy_name(build_identity_client(config, signer), tenancy_id)
    client = build_announcement_client(config, signer)

    emit_event("regions_discovered", regions=["global"])
    emit_event("region_started", region="global", phase="Listing Announcements", startedAt=iso_utc(utc_now()))
    log(f"Starting OCI announcements build with profile '{args.profile if args.auth == 'config' else 'instance_principal'}'")
    log(f"Connected to tenancy: {tenancy_name} ({tenancy_id})")
    log("Listing OCI console announcements for the tenancy root compartment")

    warnings: list[str] = []
    try:
        response = list_call_get_all_results(
            client.list_announcements,
            compartment_id=tenancy_id,
            sort_by="timeOneValue",
            sort_order="DESC",
            should_show_only_latest_in_chain=True,
        )
    except Exception as exc:
        raise SystemExit(f"Unable to list OCI console announcements: {exc}") from exc

    summaries = [] if response is None or getattr(response, "data", None) is None else list(response.data)
    emit_event("region_phase", region="global", phase="Loading Announcement Details", status="running")
    log(f"Retrieved {len(summaries)} announcement summary row(s)")

    details_by_id: dict[str, Any] = {}
    if summaries:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.max_detail_workers, len(summaries))) as executor:
            futures = [
                executor.submit(fetch_announcement_detail, client, getattr(summary, "id", "") or "")
                for summary in summaries
                if getattr(summary, "id", None)
            ]
            for future in concurrent.futures.as_completed(futures):
                announcement_id, detail, warning = future.result()
                if detail is not None:
                    details_by_id[announcement_id] = detail
                if warning:
                    warnings.append(warning)
                    log(warning)

    announcements = [
        normalize_announcement(summary, details_by_id.get(getattr(summary, "id", "") or ""))
        for summary in summaries
    ]

    payload = {
        "generatedAt": iso_utc(generated_at),
        "source": {
            "tenancyName": tenancy_name,
            "tenancyId": tenancy_id,
            "region": config.get("region", "") or "",
            "detailMode": "get_announcement enrichment when permitted",
            "latestInChainOnly": True,
        },
        "summary": {
            **build_summary(announcements),
            "detailAccessibleCount": sum(1 for item in announcements if item.get("detailSource") == "detail"),
            "warningCount": len(warnings),
        },
        "announcements": announcements,
        "warnings": warnings,
        "notes": [
            "Announcements are listed from the tenancy root compartment using OCI Announcements Service.",
            "This snapshot requests only the latest visible announcement in each chain to reduce duplicate operational updates.",
            "Announcement detail enrichment is best-effort and can vary based on IAM access to announcement details.",
        ],
    }

    args.output.write_text(json.dumps(payload, indent=2))
    emit_event(
        "region_completed",
        region="global",
        finishedAt=iso_utc(utc_now()),
        instanceCount=len(announcements),
        warningCount=len(warnings),
    )
    log(f"Wrote OCI console announcements snapshot to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
