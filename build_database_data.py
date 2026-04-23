#!/usr/bin/env python3
"""
Build fleet_data_databases.json from Oracle Cloud Infrastructure.

This collector focuses on OCI database services (Base Database, MySQL, and
PostgreSQL) so the portal can provide a database-centric operational workspace
with the same refresh pattern used by other snapshots.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import oci

from build_fleet_data import (
    choose_customer_id,
    emit_event,
    format_utc_display,
    get_tenancy_id,
    iso_utc,
    list_call_get_all_results,
    list_compartments,
    list_regions,
    load_signer_and_config,
    log,
    parse_any_datetime,
    require_oci_sdk,
    resolve_tenancy_name,
    utc_now,
)


DEFAULT_OUTPUT = "fleet_data_databases.json"
SUPPORTED_SERVICES = ("basedb", "mysql", "postgresql")
T = TypeVar("T")


@dataclass
class DatabaseArgs:
    auth: str
    config_file: str
    profile: str
    output: Path
    customer_strategy: str
    customer_tag_keys: list[str]
    timeout_seconds: float
    max_region_workers: int


def parse_args() -> DatabaseArgs:
    parser = argparse.ArgumentParser(description="Build OCI database services inventory from OCI APIs.")
    parser.add_argument("--auth", choices=["config", "instance_principal"], default="config")
    parser.add_argument("--config-file", default="~/.oci/config")
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--customer-strategy",
        choices=["tag", "compartment", "tenancy"],
        default="tenancy",
        help="How resources should be grouped into customer cards.",
    )
    parser.add_argument(
        "--customer-tag-keys",
        default="Customer,customer,ACCOUNT,account,Tenant,tenant",
        help="Comma-separated tag keys to check when customer strategy is 'tag'.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=45.0,
        help="Maximum seconds to wait for a single OCI API list call before continuing.",
    )
    parser.add_argument(
        "--max-region-workers",
        type=int,
        default=3,
        help="Maximum number of regions to process in parallel.",
    )
    ns = parser.parse_args()
    return DatabaseArgs(
        auth=ns.auth,
        config_file=ns.config_file,
        profile=ns.profile,
        output=Path(ns.output).expanduser(),
        customer_strategy=ns.customer_strategy,
        customer_tag_keys=[item.strip() for item in ns.customer_tag_keys.split(",") if item.strip()],
        timeout_seconds=max(1.0, float(ns.timeout_seconds or 45.0)),
        max_region_workers=max(1, int(ns.max_region_workers or 1)),
    )


def build_database_client(config: dict[str, Any], signer: Any, region: str):
    regional_config = dict(config)
    regional_config["region"] = region
    kwargs = {"config": regional_config}
    if signer is not None:
        kwargs["signer"] = signer
    return oci.database.DatabaseClient(**kwargs)


def build_mysql_client(config: dict[str, Any], signer: Any, region: str):
    mysql_module = getattr(oci, "mysql", None)
    client_cls = getattr(mysql_module, "DbSystemClient", None) if mysql_module else None
    if client_cls is None:
        raise RuntimeError("OCI MySQL DbSystemClient is not available in the installed OCI SDK.")
    regional_config = dict(config)
    regional_config["region"] = region
    kwargs = {"config": regional_config}
    if signer is not None:
        kwargs["signer"] = signer
    return client_cls(**kwargs)


def build_postgresql_client(config: dict[str, Any], signer: Any, region: str):
    candidates: list[type[Any]] = []
    for module_name in ("psql", "postgresql"):
        module = getattr(oci, module_name, None)
        if module is None:
            continue
        for class_name in (
            "PostgresqlClient",
            "PostgreSqlClient",
            "PostgresClient",
            "DbSystemClient",
        ):
            cls = getattr(module, class_name, None)
            if cls is not None:
                candidates.append(cls)
        for attr_name in dir(module):
            if not attr_name.endswith("Client"):
                continue
            cls = getattr(module, attr_name, None)
            if cls is not None and cls not in candidates:
                candidates.append(cls)

    regional_config = dict(config)
    regional_config["region"] = region
    kwargs = {"config": regional_config}
    if signer is not None:
        kwargs["signer"] = signer

    for cls in candidates:
        try:
            client = cls(**kwargs)
        except Exception:
            continue
        if hasattr(client, "list_db_systems") or hasattr(client, "list_postgresql_db_systems"):
            return client

    raise RuntimeError("OCI PostgreSQL client with list_db_systems support is not available in the installed OCI SDK.")


def safe_collect(label: str, fn: Callable[[], list[Any]], timeout_seconds: float) -> tuple[list[Any], str | None]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds), None
        except concurrent.futures.TimeoutError:
            return [], f"{label} exceeded {timeout_seconds:g}s"
        except Exception as exc:
            return [], f"{label} failed: {exc}"


def safe_call(label: str, fn: Callable[[], T], timeout_seconds: float) -> tuple[T | None, str | None]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds), None
        except concurrent.futures.TimeoutError:
            return None, f"{label} exceeded {timeout_seconds:g}s"
        except Exception as exc:
            return None, f"{label} failed: {exc}"


def list_across_compartments(
    config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
    timeout_seconds: float,
    client_builder: Callable[[dict[str, Any], Any, str], Any],
    method_names: tuple[str, ...],
    label: str,
) -> tuple[list[Any], list[str]]:
    warnings: list[str] = []
    items: list[Any] = []

    try:
        client = client_builder(config, signer, region)
    except Exception as exc:
        return [], [f"{label} in {region} unavailable: {exc}"]

    method = None
    selected_method_name = ""
    for candidate in method_names:
        candidate_method = getattr(client, candidate, None)
        if candidate_method is not None:
            method = candidate_method
            selected_method_name = candidate
            break
    if method is None:
        return [], [f"{label} in {region} unavailable: no supported list method ({', '.join(method_names)})."]

    for compartment_id, info in sorted(compartments.items(), key=lambda item: item[1].get("name", "")):
        compartment_name = info.get("name", compartment_id)

        def call() -> list[Any]:
            response = list_call_get_all_results(method, compartment_id=compartment_id)
            if response is None or getattr(response, "data", None) is None:
                return []
            return list(response.data)

        collected, warning = safe_collect(
            f"{label} ({selected_method_name}) in {region} for compartment {compartment_name} ({compartment_id})",
            call,
            timeout_seconds,
        )
        if warning:
            warnings.append(warning)
            continue
        items.extend(collected)

    return items, warnings


def derive_mysql_maintenance_values(item: Any) -> tuple[str, str]:
    maintenance = getattr(item, "maintenance", None)
    if maintenance is None:
        return "", ""

    scheduled_dt = parse_any_datetime(getattr(maintenance, "time_scheduled", None))
    if scheduled_dt is not None:
        return "SCHEDULED", format_utc_display(scheduled_dt)

    configured_window = _pick(maintenance, ("window_start_time",), "")
    if configured_window:
        return "WINDOW_CONFIGURED", str(configured_window)

    return "", ""


def enrich_mysql_rows_with_db_details(
    config: dict[str, Any],
    signer: Any,
    region: str,
    rows: list[dict[str, Any]],
    compartments: dict[str, dict[str, str]],
    timeout_seconds: float,
) -> list[str]:
    warnings: list[str] = []
    if not rows:
        return warnings

    try:
        client = build_mysql_client(config, signer, region)
    except Exception as exc:
        return [f"MySQL DB system detail lookup in {region} unavailable: {exc}"]

    get_method = getattr(client, "get_db_system", None)
    if get_method is None:
        return [f"MySQL DB system detail lookup in {region} unavailable: no get_db_system method."]

    for row in rows:
        db_system_id = str(row.get("id") or "").strip()
        if not db_system_id:
            continue

        detail, warning = safe_call(
            f"MySQL DB system detail lookup in {region} for {db_system_id}",
            lambda db_system_id=db_system_id: get_method(db_system_id).data,
            timeout_seconds,
        )
        if warning:
            warnings.append(warning)
            continue
        if detail is None:
            continue

        row["shapeOrTier"] = row.get("shapeOrTier") or _pick(detail, ("shape_name", "shape"), "")
        row["engineVersion"] = row.get("engineVersion") or _pick(detail, ("mysql_version", "version"), "")
        row["subnetId"] = row.get("subnetId") or _pick(detail, ("subnet_id",), "")
        row["ocpu"] = row.get("ocpu") if row.get("ocpu") is not None else _to_float(_pick(detail, ("cpu_core_count", "ocpu_count"), None))
        row["memoryGb"] = row.get("memoryGb") if row.get("memoryGb") is not None else _to_float(_pick(detail, ("memory_size_in_gbs", "memory_size_in_gb"), None))
        row["storageGb"] = row.get("storageGb") if row.get("storageGb") is not None else _to_float(_pick(detail, ("data_storage_size_in_gbs", "data_storage_size_in_gb"), None))

        detail_compartment_id = _pick(detail, ("compartment_id",), "")
        if detail_compartment_id:
            row["compartmentId"] = detail_compartment_id
            row["compartmentName"] = compartments.get(detail_compartment_id, {}).get("name", row.get("compartmentName", "Unknown"))

        derived_status, derived_window = derive_mysql_maintenance_values(detail)
        if derived_status:
            row["maintenanceStatus"] = derived_status
        if derived_window:
            row["maintenanceWindowUtc"] = derived_window

    return warnings


def _pick(item: Any, names: tuple[str, ...], default: Any = "") -> Any:
    for name in names:
        value = getattr(item, name, None)
        if value not in (None, ""):
            return value
    return default


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _to_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "enabled", "ha"}:
            return True
        if lowered in {"false", "no", "0", "disabled"}:
            return False
    return None


def normalize_database_row(
    args: DatabaseArgs,
    item: Any,
    service_type: str,
    region: str,
    tenancy_id: str,
    tenancy_name: str,
    generated_at_text: str,
    compartments: dict[str, dict[str, str]],
) -> dict[str, Any]:
    resource_id = _pick(item, ("id",))
    display_name = _pick(item, ("display_name", "name"), resource_id)
    compartment_id = _pick(item, ("compartment_id",))
    compartment_name = compartments.get(compartment_id, {}).get("name", "Unknown")
    freeform_tags = _pick(item, ("freeform_tags",), {}) or {}
    defined_tags = _pick(item, ("defined_tags",), {}) or {}
    customer_id = choose_customer_id(args, tenancy_name, compartment_name, freeform_tags, defined_tags)

    shape_or_tier = _pick(item, ("shape", "shape_name", "instance_class", "sku_name"), "")
    engine_version = _pick(item, ("db_version", "mysql_version", "postgresql_version", "database_version", "version"), "")
    lifecycle_state = _pick(item, ("lifecycle_state", "state"), "")

    ocpu = _to_float(_pick(item, ("cpu_core_count", "ocpu_count", "instance_ocpu_count"), None))
    memory_gb = _to_float(_pick(item, ("memory_size_in_gbs", "memory_size_in_gb", "instance_memory_size_in_gbs"), None))
    storage_gb = _to_float(_pick(item, ("data_storage_size_in_gbs", "storage_size_in_gbs", "data_storage_size_in_gb"), None))
    storage_tb = _to_float(_pick(item, ("data_storage_size_in_tbs",), None))
    if storage_gb is None and storage_tb is not None:
        storage_gb = storage_tb * 1024

    maintenance_window_dt = parse_any_datetime(_pick(item, ("maintenance_window_start", "time_maintenance_window_start", "next_maintenance_run"), None))
    maintenance_window_utc = format_utc_display(maintenance_window_dt)
    maintenance_status = _pick(item, ("maintenance_status", "maintenance_state", "maintenanceState"), "")

    created_dt = parse_any_datetime(_pick(item, ("time_created", "created_at"), None))
    time_created_utc = format_utc_display(created_dt)

    is_ha = _to_bool(_pick(item, ("is_highly_available", "is_ha", "highly_available"), None))

    row = {
        "customerId": customer_id,
        "id": resource_id,
        "displayName": display_name,
        "serviceType": service_type,
        "lifecycleState": lifecycle_state,
        "region": region,
        "tenancyId": tenancy_id,
        "compartmentId": compartment_id,
        "compartmentName": compartment_name,
        "availabilityDomain": _pick(item, ("availability_domain",), ""),
        "faultDomain": _pick(item, ("fault_domain",), ""),
        "subnetId": _pick(item, ("subnet_id",), ""),
        "shapeOrTier": shape_or_tier,
        "engineVersion": engine_version,
        "ocpu": ocpu,
        "memoryGb": memory_gb,
        "storageGb": storage_gb,
        "isHighlyAvailable": is_ha,
        "maintenanceStatus": maintenance_status,
        "maintenanceWindowUtc": maintenance_window_utc,
        "timeCreatedUtc": time_created_utc,
        "lastSeenUtc": generated_at_text,
        "freeformTags": freeform_tags,
        "definedTags": defined_tags,
        "uniqueKey": f"{service_type}:{resource_id}",
    }
    return row


def process_region(
    args: DatabaseArgs,
    config: dict[str, Any],
    signer: Any,
    region: str,
    compartments: dict[str, dict[str, str]],
    tenancy_id: str,
    tenancy_name: str,
    generated_at_text: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    emit_event("region_started", region=region, phase="Starting", startedAt=iso_utc(utc_now()))

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    emit_event("region_phase", region=region, phase="Collecting Base Database", status="running")
    basedb_items, basedb_warnings = list_across_compartments(
        config,
        signer,
        region,
        compartments,
        args.timeout_seconds,
        build_database_client,
        ("list_db_systems",),
        "Base Database listing",
    )
    warnings.extend(basedb_warnings)
    rows.extend(
        normalize_database_row(args, item, "basedb", region, tenancy_id, tenancy_name, generated_at_text, compartments)
        for item in basedb_items
    )

    emit_event("region_phase", region=region, phase="Collecting MySQL", status="running")
    mysql_items, mysql_warnings = list_across_compartments(
        config,
        signer,
        region,
        compartments,
        args.timeout_seconds,
        build_mysql_client,
        ("list_db_systems",),
        "MySQL DB system listing",
    )
    warnings.extend(mysql_warnings)
    mysql_rows = [
        normalize_database_row(args, item, "mysql", region, tenancy_id, tenancy_name, generated_at_text, compartments)
        for item in mysql_items
    ]
    warnings.extend(
        enrich_mysql_rows_with_db_details(
            config,
            signer,
            region,
            mysql_rows,
            compartments,
            args.timeout_seconds,
        )
    )
    rows.extend(mysql_rows)

    emit_event("region_phase", region=region, phase="Collecting PostgreSQL", status="running")
    postgres_items, postgres_warnings = list_across_compartments(
        config,
        signer,
        region,
        compartments,
        args.timeout_seconds,
        build_postgresql_client,
        ("list_db_systems", "list_postgresql_db_systems"),
        "PostgreSQL DB system listing",
    )
    warnings.extend(postgres_warnings)
    rows.extend(
        normalize_database_row(args, item, "postgresql", region, tenancy_id, tenancy_name, generated_at_text, compartments)
        for item in postgres_items
    )

    for warning in warnings:
        log(warning)

    emit_event(
        "region_completed",
        region=region,
        finishedAt=iso_utc(utc_now()),
        instanceCount=len(rows),
    )
    log(f"Region {region} contributed {len(rows)} database service row(s)")
    return rows, warnings


def build_customers(rows: list[dict[str, Any]], generated_at_epoch_ms: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["customerId"]].append(row)

    customers: list[dict[str, Any]] = []
    for customer_id, items in sorted(grouped.items()):
        service_counts = Counter(item.get("serviceType", "") for item in items)
        customers.append(
            {
                "name": customer_id,
                "lastImport": generated_at_epoch_ms,
                "resourceCount": len(items),
                "basedbCount": service_counts.get("basedb", 0),
                "mysqlCount": service_counts.get("mysql", 0),
                "postgresqlCount": service_counts.get("postgresql", 0),
            }
        )
    return customers


def build_summary(rows: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    service_counts = Counter(row.get("serviceType", "") for row in rows)
    lifecycle_counts = Counter((row.get("lifecycleState") or "Unknown") for row in rows)
    region_count = len({row.get("region") for row in rows if row.get("region")})
    compartment_count = len({row.get("compartmentId") for row in rows if row.get("compartmentId")})
    return {
        "totalCount": len(rows),
        "serviceCounts": {
            "basedb": int(service_counts.get("basedb", 0)),
            "mysql": int(service_counts.get("mysql", 0)),
            "postgresql": int(service_counts.get("postgresql", 0)),
        },
        "lifecycleCounts": {key: int(value) for key, value in sorted(lifecycle_counts.items())},
        "regionCount": region_count,
        "compartmentCount": compartment_count,
        "warningCount": len(warnings),
    }


def main() -> int:
    args = parse_args()
    require_oci_sdk()

    try:
        config, signer = load_signer_and_config(args)
    except Exception as exc:
        print(
            "Unable to initialize OCI authentication for the database services scan.\n"
            "If you are running locally, create ~/.oci/config or pass --config-file.\n"
            "If you are running on OCI compute, try --auth instance_principal.\n"
            f"Details: {exc}",
            flush=True,
        )
        return 1

    generated_at = utc_now()
    generated_at_text = format_utc_display(generated_at)
    generated_at_epoch_ms = int(generated_at.timestamp() * 1000)

    tenancy_id = get_tenancy_id(config, signer)
    identity_client_kwargs = {"config": config}
    if signer is not None:
        identity_client_kwargs["signer"] = signer
    identity_client = oci.identity.IdentityClient(**identity_client_kwargs)

    tenancy_name = resolve_tenancy_name(identity_client, tenancy_id)
    compartments = list_compartments(identity_client, tenancy_id)
    regions = list_regions(identity_client, tenancy_id)
    log(f"Starting OCI database services build with profile '{args.profile if args.auth == 'config' else 'instance_principal'}'")
    log(f"Scanning subscribed regions for database services: {', '.join(regions)}")
    emit_event("regions_discovered", regions=regions)

    all_rows: list[dict[str, Any]] = []
    all_warnings: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_region_workers) as executor:
        future_by_region = {
            executor.submit(
                process_region,
                args,
                config,
                signer,
                region,
                compartments,
                tenancy_id,
                tenancy_name,
                generated_at_text,
            ): region
            for region in regions
        }
        for future in concurrent.futures.as_completed(future_by_region):
            region = future_by_region[future]
            try:
                region_rows, region_warnings = future.result()
                all_rows.extend(region_rows)
                all_warnings.extend(region_warnings)
            except Exception as exc:
                warning = f"Region {region} failed unexpectedly during database services scan: {exc}"
                all_warnings.append(warning)
                log(warning)
                emit_event(
                    "region_failed",
                    region=region,
                    phase="Failed",
                    finishedAt=iso_utc(utc_now()),
                )

    all_rows.sort(key=lambda row: (row.get("customerId", ""), row.get("serviceType", ""), row.get("region", ""), row.get("displayName", ""), row.get("id", "")))

    payload = {
        "schemaVersion": 1,
        "generatedAt": generated_at_text,
        "generatedAtEpochMs": generated_at_epoch_ms,
        "source": {
            "tenancyName": tenancy_name,
            "tenancyId": tenancy_id,
            "authMode": args.auth,
            "profile": args.profile if args.auth == "config" else "instance_principal",
            "regionCount": len(regions),
            "regions": regions,
            "customerStrategy": args.customer_strategy,
            "services": list(SUPPORTED_SERVICES),
        },
        "summary": build_summary(all_rows, all_warnings),
        "customers": build_customers(all_rows, generated_at_epoch_ms),
        "databases": all_rows,
        "warnings": sorted(set(all_warnings)),
    }

    args.output.write_text(json.dumps(payload, indent=2))
    log(f"Wrote OCI database services snapshot with {len(all_rows)} row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
