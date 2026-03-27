#!/usr/bin/env python3
"""Serve the OCI portal locally and expose a refresh endpoint for fleet_data.json."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
BUILD_SCRIPT = ROOT / "build_fleet_data.py"
FLEET_JSON = ROOT / "fleet_data.json"
UTC = timezone.utc
EVENT_PREFIX = "__OTX_EVENT__ "


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    def __init__(self, profile: str, config_file: str, auth: str) -> None:
        self.profile = profile
        self.config_file = config_file
        self.auth = auth
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
            str(BUILD_SCRIPT),
            "--profile",
            self.profile,
            "--config-file",
            self.config_file,
            "--output",
            str(FLEET_JSON),
        ]
        if self.auth != "config":
            command.extend(["--auth", self.auth])

        self.state.append(f"[{utc_now_iso()}] Launching OCI collector")
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the OCI Tenancy Explorer locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--config-file", default=str(Path("~/.oci/config").expanduser()))
    parser.add_argument("--auth", choices=["config", "instance_principal"], default="config")
    return parser.parse_args()


def make_handler(runner: RefreshRunner):
    class PortalHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/refresh/status":
                self._send_json(runner.state.snapshot())
                return
            super().do_GET()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/refresh":
                started, message = runner.start()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json({"started": started, "message": message, **runner.state.snapshot()}, status=status)
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
    runner = RefreshRunner(profile=args.profile, config_file=args.config_file, auth=args.auth)
    handler = make_handler(runner)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/index.html"
    print(f"Serving OCI Tenancy Explorer at {url}")
    print(f"Refresh endpoint uses profile '{args.profile}' and config '{args.config_file}'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down portal server...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
