# OCI Tenancy Explorer

OCI Tenancy Explorer is a lightweight single-page application for displaying OCI compute instance maintenance and operational signals in a customer-friendly dashboard.

## Executive Summary

OCI Tenancy Explorer provides a simple way for an OCI customer or operations team to answer a specific question:

`Which compute instances are scheduled for maintenance reboot, and when did that status last change?`

The solution is intentionally lightweight:

- a single HTML dashboard for the user experience
- a Python collector for OCI data retrieval
- a generated JSON file as the system-of-record snapshot
- a small local portal server for one-click refresh from the UI

This makes the application easy to share, easy to understand, and easy to deploy into a customer environment without introducing a full web application stack.

Typical use cases include:

- platform operations teams monitoring tenancy-wide reboot-related maintenance signals
- customer success and support teams demonstrating live fleet visibility
- customer administrators who want a portable OCI dashboard without building a custom backend
- demo environments where operators need to refresh on demand and show live OCI data in a controlled UI

The application is designed to:

- discover compute instances across all subscribed regions in an OCI tenancy
- present the tenancy as the default top-level view, with optional grouping overrides if needed
- highlight maintenance reboot status changes over time
- export the current fleet snapshot as JSON or CSV
- provide a one-click refresh flow from the UI without exposing OCI credentials in the browser

The project keeps the front end static and moves OCI access into a local Python collector.

## What This Project Includes

- `index.html`
  The customer-facing SPA dashboard.
- `build_fleet_data.py`
  The OCI collector that queries the tenancy and writes `fleet_data.json`.
- `portal_server.py`
  A lightweight local web server that serves the dashboard and exposes refresh endpoints used by the UI.
- `fleet_data.json`
  The live generated fleet snapshot consumed by the dashboard.
- `fleet_data.sample.json`
  A sanitized example payload for documentation, demos, and safe sharing.
- `oci_config.example`
  Example OCI SDK config file format.

## 5-Minute Quick Start

If you already have OCI API access configured, this is the fastest path to a working deployment.

1. Open a terminal and change into the project folder.
2. Install the OCI Python SDK.
3. Generate the fleet snapshot.
4. Start the portal server.
5. Open the dashboard in the browser.

If you are running locally in VS Code, open the project folder first and use the integrated terminal for the commands below.

Commands:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 -m pip install oci
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
python3 portal_server.py --profile DEFAULT
```

Open:

```text
http://127.0.0.1:8765/index.html
```

Then click:

```text
Refresh Data
```

If you do not yet have OCI authentication configured, continue to the `OCI Authentication Setup` section below.

## High-Level Architecture

The application works in three layers:

1. `build_fleet_data.py` connects to OCI using the Python SDK.
2. It scans all subscribed regions in the tenancy and writes a normalized `fleet_data.json`.
3. `index.html` loads `fleet_data.json` into in-memory client state and renders the dashboard.

For one-click refresh in the UI:

1. The browser calls `POST /api/refresh` on `portal_server.py`.
2. `portal_server.py` launches `build_fleet_data.py` in the background.
3. The browser polls `GET /api/refresh/status`.
4. The modal displays live collector log output.
5. When the refresh completes successfully, the dashboard reloads the updated `fleet_data.json`.

This design is intentional. OCI credentials stay on the local machine or execution host and are never exposed to browser JavaScript.

## Operational Framework

This section mirrors the in-application documentation and can be reused directly in a customer recipe, deployment guide, or whitepaper.

### What This Application Does

OCI Tenancy Explorer gives administrators and customers a clear view of compute instances across the tenancy, including maintenance reboot dates, reboot-related evidence, and recent status changes.

- Fleet-wide view:
  The collector discovers OCI compute instances across all subscribed regions marked as ready in the tenancy.
- Reboot visibility:
  The dashboard shows whether an instance is scheduled, completed, or not scheduled for a reboot-related maintenance event.
- Change tracking:
  The application compares the latest snapshot with the previous one so operators can quickly spot transitions.
- Safe browser model:
  OCI credentials stay on the host running the collector and are never exposed to the browser.

### How It Is Installed

The customer installation footprint is intentionally small.

1. Install Python 3 and the OCI Python SDK.
2. Create OCI authentication in `~/.oci/config` or use instance principals on OCI compute.
3. Run `portal_server.py` on the same host that will run the collector and serve `fleet_data.json`.
4. Use the portal refresh flow or run `build_fleet_data.py` on that same host to generate `fleet_data.json`.
5. Open the dashboard locally and refresh on demand from the UI.

Typical launch flow:

- `portal_server.py` serves the application.
- `build_fleet_data.py` runs on that same host and creates the JSON snapshot.
- the browser reads the generated file from that same host and renders the fleet view.
- this same-host model is what enables the built-in one-click refresh flow from the UI.

### How OCI Authentication Works

Customers have two supported authentication models.

#### Option 1: Local OCI Config File

This is the simplest model for a laptop or workstation deployment.

The customer creates `~/.oci/config` with:

- `user`
  The OCI user OCID that owns the API key.
- `tenancy`
  The tenancy OCID.
- `fingerprint`
  The fingerprint of the uploaded public API key.
- `region`
  Any valid starting region. Only one region is required here.
- `key_file`
  The full path to the private key file on disk.

Example:

```ini
[DEFAULT]
user=ocid1.user.oc1..your_user_ocid_here
fingerprint=aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00
tenancy=ocid1.tenancy.oc1..your_tenancy_ocid_here
region=us-ashburn-1
key_file=/Users/user_name/.oci/oci_api_key.pem
```

The customer must also:

1. generate an OCI API key pair
2. upload the public key in the OCI Console under the user account
3. place the private key at the path referenced by `key_file`
4. ensure the file permissions are appropriately restricted

Important note:

- the collector discovers all subscribed regions automatically
- the `region` field in the config is only the starting region used for authentication and the initial OCI calls

#### Option 2: Instance Principals

This is the preferred model when the collector runs on an OCI compute instance.

In this model:

- no local API key file is required
- the compute instance is placed in a dynamic group
- IAM policies grant the instance permission to inspect compartments and read the required OCI services
- `portal_server.py`, `build_fleet_data.py`, and `fleet_data.json` all stay on that OCI host

The commands become:

```bash
python3 build_fleet_data.py --auth instance_principal --output fleet_data.json
python3 portal_server.py --auth instance_principal
```

This is usually the best long-term customer deployment model for a shared environment.

### Live OCI Refresh Flow

The dashboard supports a controlled one-click refresh model that is safe for customer environments and easy for operators to understand.

- Refresh button:
  The user clicks `Refresh Data` inside the portal.
- Local endpoint:
  `portal_server.py` starts the collector in the background and streams live log output to the modal.
- Parallel region processing:
  The collector can process multiple subscribed regions in parallel so larger tenancies complete refreshes faster without waiting for each region to finish serially.
- Progress visibility:
  The refresh console shows overall progress, region activity, per-region logs, and the combined collector log so the user can see which regions are queued, running, completed, or failed during the refresh.
- JSON rebuild:
  The collector rewrites `fleet_data.json` with the newest tenancy snapshot.
- Browser sync:
  After `fleet_data.json` is updated on the host, the SPA reloads that refreshed snapshot into memory and updates the grid so added, removed, or changed rows are reflected on the next load.

### Status Meanings

The application compares the latest OCI snapshot with the previous one to show the current reboot state in a way that is readable for both engineers and business users.

- `Scheduled`
  A maintenance reboot date is currently present in OCI.
- `Completed`
  The previous snapshot showed a maintenance reboot date, but the current snapshot no longer does.
- `Not Scheduled`
  No maintenance reboot signal is currently reported by OCI for the instance.

### Change Signals

The dashboard highlights what changed since the previous collector run so a reboot-related transition is easy to see immediately.

- Signal column:
  Shows `Stable` or a transition such as `Not Scheduled -> Scheduled`.
- Changed stat:
  Counts how many rows changed in the current filtered view.
- Customer card highlight:
  Fleet cards glow blue when any instance in that customer changed.

### OCI Data Sources

The maintenance and reboot fields shown in the grid are based on direct OCI API data and snapshot comparison logic.

- Identity service:
  Used to list accessible compartments and subscribed regions. This determines which compartments and regions are scanned.
- Compute service:
  Used to list instances and active `InstanceMaintenanceEvent` records per compartment and region. Maintenance dates, actions, and event status now come primarily from instance maintenance events, with the OCI instance field `time_maintenance_reboot_due` kept as a fallback.
- OS Management Hub:
  When available, used to read managed instance boot data to populate `Last_Reboot_UTC` and set `Reboot_Evidence`.
- Availability configuration:
  Used to surface `Live_Migration_Preference` so the dashboard can show whether Oracle can prefer live migration instead of a reboot-based maintenance path.

### How Maintenance Dates Are Determined

The maintenance date logic is intentionally simple and traceable:

1. The collector reads OCI compute instances and then queries `InstanceMaintenanceEvent` records for the same compartments and regions.
2. If an active maintenance event exists, the collector uses that event as the primary maintenance source.
3. The event timing is written into `Maintenance_UTC`, `Maintenance_IST`, and `Maintenance_EDT`, and the event action is written into `Maintenance_Action`.
4. If no active maintenance event exists, the collector falls back to the instance field `time_maintenance_reboot_due`.
5. If a reboot-based maintenance schedule disappears on a later refresh, the application compares the previous snapshot and marks the instance as `Completed`.
6. If no maintenance date is present and there is no prior scheduled signal, the application marks the instance as `Not Scheduled`.

### Timezones And Evidence

- `UTC`
  Primary timestamp reference returned by OCI and used for change tracking.
- `IST`
  Derived from UTC using an automated `+5:30` offset.
- `EDT`
  Derived from UTC using an automated `-4:00` offset.
- `Reboot_Evidence`
  Shows whether the displayed reboot context came from OCI maintenance data, OS Management Hub boot data, or prior snapshot comparison logic.

## Deployment Diagram

### Local Admin Deployment

```text
+--------------------+        POST /api/refresh        +----------------------+
|  Browser / User    | ----------------------------->  |   portal_server.py   |
|  index.html        |                                 |  local HTTP server   |
|  Dashboard UI      | <-----------------------------  |  serves UI + status  |
+--------------------+      GET /api/refresh/status    +----------+-----------+
                                                                     |
                                                                     | launches
                                                                     v
                                                          +----------------------+
                                                          |  build_fleet_data.py |
                                                          |  OCI Python SDK      |
                                                          +----------+-----------+
                                                                     |
                                                                     | reads OCI
                                                                     v
                                                          +----------------------+
                                                          | Oracle Cloud         |
                                                          | Identity / Compute / |
                                                          | OSMH regional APIs   |
                                                          +----------+-----------+
                                                                     |
                                                                     | writes
                                                                     v
                                                          +----------------------+
                                                          |   fleet_data.json    |
                                                          |   live fleet file    |
                                                          +----------------------+
```

### Recommended Customer Shared Deployment

```text
+----------------------+        serves static UI        +----------------------+
| Customer End Users   | <----------------------------> |  Web Host / Portal   |
| Browsers             |                                |  index.html          |
+----------------------+                                +----------+-----------+
                                                                     |
                                                                     | reads
                                                                     v
                                                          +----------------------+
                                                          |   fleet_data.json    |
                                                          | scheduled snapshot   |
                                                          +----------+-----------+
                                                                     ^
                                                                     | writes on schedule
                                                          +----------+-----------+
                                                          | Collector Host       |
                                                          | build_fleet_data.py  |
                                                          | instance principal   |
                                                          | or API-key auth      |
                                                          +----------+-----------+
                                                                     |
                                                                     v
                                                          +----------------------+
                                                          | Oracle Cloud         |
                                                          | multi-region APIs    |
                                                          +----------------------+
```

### Why This Model Works Well

- the dashboard remains static and easy to host
- credentials remain outside the browser
- the collector can run on demand or on a schedule
- the generated JSON can be archived for auditing or comparison
- customers can choose a local, OCI-hosted, or shared deployment pattern

## Key Features

- multi-region tenancy discovery
- tag-based or compartment-based customer grouping
- maintenance reboot status tracking
- previous-versus-current status comparison
- status change signal reporting
- live JSON source indicator in the UI
- JSON generation timestamp displayed in the header
- live refresh console with streaming logs
- CSV export of the currently filtered grid

## Supported Data Signals

The dashboard currently derives status from:

- active `InstanceMaintenanceEvent` records from the OCI Compute API
- `time_maintenance_reboot_due` on the OCI compute instance
- OS Management Hub managed instance last boot time, when available
- previous snapshot comparison for change detection

The UI-friendly status values are:

- `Scheduled`
- `Completed`
- `Not Scheduled`

## Requirements

- macOS, Linux, or another environment capable of running Python 3
- Python 3.10+ recommended
- OCI Python SDK installed
- an OCI API key and config file, or an OCI compute instance using instance principals
- network access to the relevant OCI regional endpoints
- for the built-in `Refresh Data` flow, `portal_server.py`, `build_fleet_data.py`, and `fleet_data.json` must live on the same host

## Python Dependency

Install the OCI SDK:

```bash
python3 -m pip install oci
```

If you also want the OCI CLI for validation and troubleshooting:

```bash
python3 -m pip install oci-cli
```

## Folder Layout

This README assumes the project is located at:

```bash
"/path/to/OCI Tenancy Explorer"
```

If you place it somewhere else, the commands remain the same after you `cd` into the project directory.

## Deployment Options

There are three practical ways to deploy this project.

### Option 1: Local Laptop or Desktop

Best for demos, admin use, and low-friction customer handoff.

- store OCI credentials in `~/.oci/config`
- run `portal_server.py` on the same machine that will generate `fleet_data.json`
- open the dashboard in a browser on `127.0.0.1:8765`
- use `Refresh Data` in the header when you want the same machine to rebuild the snapshot from OCI

### Option 2: OCI Compute Instance

Best for a more persistent customer deployment.

- install Python and the OCI SDK on the instance
- use instance principals instead of a local API key if possible
- run `portal_server.py` and `build_fleet_data.py` on that same OCI instance
- run the collector on a schedule if you want periodic refresh in addition to manual refresh
- optionally reverse proxy the UI if you want shared access

### Option 3: OCI Cloud Shell or Another Managed OCI Host

Best when a user laptop has inconsistent network routing to specific OCI regions.

- run the collector in Cloud Shell or on an OCI host
- generate `fleet_data.json` there
- serve or distribute the JSON to the front end
- note that the built-in `Refresh Data` button only works when the portal server and collector are running together on the same host

This option is especially useful if corporate VPN or local routing interferes with regional OCI endpoints.

## Recommended Customer Deployment Patterns

### Small Team or Single Administrator

Use local deployment.

- simplest to stand up
- minimal infrastructure required
- ideal for demos, pilots, and admin-led refresh

### Shared Operations Team

Use an OCI compute instance or other managed host.

- centralizes access to OCI APIs
- avoids dependency on one admin laptop
- allows scheduled fleet refresh
- avoids local VPN or routing issues

### Customer-Facing Shared Dashboard

Use a split model:

- run `build_fleet_data.py` on a trusted backend host
- publish `fleet_data.json` on a schedule
- serve `index.html` and `fleet_data.json` to end users through a controlled web tier
- if the UI and collector are split across different hosts, treat the JSON as a published snapshot and do not rely on one-click browser refresh

This is usually the cleanest production model.

## Installation Paths

Choose one of these two supported deployment patterns.

### Path A: Local Workstation With `~/.oci/config`

Use this when an administrator wants to run the tool locally from a laptop or desktop against an OCI tenancy.

1. Install Python 3.10+.
2. Install the OCI SDK:

```bash
python3 -m pip install oci
```

3. Create the local OCI config directory and permissions:

```bash
mkdir -p ~/.oci
chmod 700 ~/.oci
```

4. Create `~/.oci/config` and place the matching private key on disk.
5. Generate the first fleet snapshot:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
```

6. Start the portal on that same machine:

```bash
python3 portal_server.py --profile DEFAULT
```

7. Open:

```text
http://127.0.0.1:8765/index.html
```

8. Use `Refresh Data` in the header any time you want to rebuild the local snapshot from OCI.

### Path B: OCI Compute Instance With Instance Principals

Use this when the customer wants the portal and collector to run inside OCI and avoid distributing local API keys.

1. Provision an OCI compute instance that will host both the portal and the collector.
2. Install Python 3.10+ and the OCI SDK on that instance.
3. Add the instance to the correct dynamic group.
4. Add IAM policies so the instance principal can inspect compartments, read region subscriptions, list compute instances, read instance maintenance events, and optionally query OS Management Hub if reboot evidence is needed.
5. Generate the first fleet snapshot directly on the OCI host:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 build_fleet_data.py --auth instance_principal --output fleet_data.json
```

6. Start the portal on that same OCI host:

```bash
python3 portal_server.py --auth instance_principal
```

7. Open the portal through the host address or the internal reverse-proxy address exposed by the customer.

This is the preferred deployment model for a shared internal environment because OCI credentials remain server-side and do not need to be copied to user workstations.

## OCI Authentication Setup

### Local API Key Method

Create the OCI config directory:

```bash
mkdir -p ~/.oci
chmod 700 ~/.oci
```

Generate or place your private API key at a path such as:

```bash
~/.oci/oci_api_key.pem
```

Then create `~/.oci/config` using the format in `oci_config.example`.

Example:

```ini
[DEFAULT]
user=ocid1.user.oc1..your_user_ocid_here
fingerprint=aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00
tenancy=ocid1.tenancy.oc1..your_tenancy_ocid_here
region=us-ashburn-1
key_file=/Users/user_name/.oci/oci_api_key.pem
```

Important notes:

- `region=` only needs to be a single valid region
- the collector will discover all subscribed regions automatically
- `key_file` must point to a real file on disk
- the API key must be uploaded in OCI under the matching user
- the OCI user must have API key authentication enabled and sufficient IAM access to inspect the target resources

Detailed customer setup flow:

1. Open the OCI Console and navigate to the target user.
2. Generate or upload an API public key for that user.
3. Copy the displayed fingerprint into `~/.oci/config`.
4. Save the private key locally at the `key_file` path.
5. Confirm the `user` OCID and `tenancy` OCID are correct.
6. Run `build_fleet_data.py` once to validate authentication before launching the UI.

Suggested key permissions:

```bash
chmod 600 ~/.oci/oci_api_key.pem
chmod 600 ~/.oci/config
```

### Instance Principal Method

If running on an OCI compute instance, you can avoid local config files:

```bash
python3 build_fleet_data.py --auth instance_principal --output fleet_data.json
```

For the portal server:

```bash
python3 portal_server.py --auth instance_principal
```

The instance must be in a dynamic group with IAM policies allowing the required OCI read operations.

## Minimum OCI Permissions

The collector needs read access to:

- tenancy compartments
- region subscriptions
- compute instances
- OS Management Hub managed instances if reboot evidence is desired

At a high level, the executing identity must be able to inspect compartments and read compute resources across the target scope.

## First-Time Local Setup

1. Open a terminal.
2. Change into the project folder.
3. Install the OCI SDK.
4. Configure `~/.oci/config`.
5. Run the collector once to generate `fleet_data.json`.
6. Start the local portal server.
7. Open the dashboard in the browser.

Expanded authentication checklist:

- create `~/.oci`
- place `config` inside that directory
- place the private key file referenced by `key_file` on disk
- confirm the uploaded public key in OCI matches the private key on disk
- confirm the `fingerprint` in `config` matches the OCI Console entry
- confirm the `user` and `tenancy` OCIDs are correct

Commands:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 -m pip install oci
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
python3 portal_server.py --profile DEFAULT
```

Then browse to:

```text
http://127.0.0.1:8765/index.html
```

At that point the application is ready to use.

## Running the Collector Manually

To rebuild the JSON snapshot without starting the UI server:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
```

Useful variants:

```bash
python3 build_fleet_data.py --profile DEFAULT --config-file ~/.oci/config --output fleet_data.json
python3 build_fleet_data.py --auth instance_principal --output fleet_data.json
python3 build_fleet_data.py --profile DEFAULT --max-region-workers 3 --output fleet_data.json
python3 build_fleet_data.py --profile DEFAULT --customer-strategy compartment --output fleet_data.json
python3 build_fleet_data.py --profile DEFAULT --include-terminated --output fleet_data.json
```

Notes:

- `--max-region-workers` controls how many OCI regions are processed in parallel during a refresh.
- The default is intentionally conservative so the collector is faster without being overly aggressive against OCI APIs.

## Running the Portal Server

Start the local portal:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 portal_server.py --profile DEFAULT
```

By default it serves:

```text
http://127.0.0.1:8765/index.html
```

Optional flags:

```bash
python3 portal_server.py --host 127.0.0.1 --port 8765 --profile DEFAULT
python3 portal_server.py --config-file /full/path/to/config --profile DEFAULT
python3 portal_server.py --auth instance_principal
```

## Running With Docker

You can run the portal and collector in a container instead of a local Python environment.

Build and run directly:

```bash
docker build -t oci-tenancy-explorer .
docker run --rm -p 8765:8765 \
  -e OCI_AUTH=config \
  -e OCI_PROFILE=<DEFAULT> \
  -e OCI_CONFIG_FILE=/home/appuser/.oci/config \
  -v ~/.oci:/home/appuser/.oci:ro \
  -v ~/.ssh:/home/appuser/.ssh:ro \
  oci-tenancy-explorer
```

Then open:

```text
http://127.0.0.1:8765/index.html
```

## Running With Docker Compose

The repository includes `docker-compose.yml`.

Start:

```bash
docker compose up --build
```

If the container is running on an OCI compute instance and you want instance principal auth, use the OCI override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.oci-instance-principal.yml up --build
```

That override sets:

- `OCI_AUTH=instance_principal`
- `network_mode: host` (so instance metadata service access is available to the container)
- no host key/config volume mounts

The default compose file assumes local API-key auth (`OCI_AUTH=config`) and mounts:

- `${HOME}/.oci` to `/home/appuser/.oci` (for OCI config)
- `${HOME}/.ssh` to `/home/appuser/.ssh` (for private key paths referenced by `key_file`)

### What To Change In `docker-compose.yml`

Use these edits depending on your auth model:

1. Local API key (`~/.oci/config`)
- Keep:
  - `OCI_AUTH: config`
  - `OCI_PROFILE: DEFAULT` (or your profile)
  - `OCI_CONFIG_FILE: /home/appuser/.oci/config`
  - both `.oci` and `.ssh` volume mounts
- If your `key_file` in OCI config points somewhere else, update the mount and `OCI_CONFIG_FILE` so that path exists inside the container.

2. OCI Compute Instance Principal
- Change:
  - `OCI_AUTH: instance_principal`
- Optional cleanup:
  - remove `.oci` and `.ssh` mounts if not needed
- Keep port mapping so the UI is reachable externally.

Example minimal environment block for instance principal:

```yaml
environment:
  OCI_AUTH: instance_principal
  OCI_PROFILE: DEFAULT
  OCI_CONFIG_FILE: /home/appuser/.oci/config
  PORT: "8765"
```

> Note: `OCI_CONFIG_FILE` is ignored when `OCI_AUTH=instance_principal`.

## Correct Way to Open the UI

Use:

```text
http://127.0.0.1:8765/index.html
```

Do not use:

- `file://...`
- IDE Live Server URLs such as `127.0.0.1:5500`
- a stale browser tab from a different local server

The one-click OCI refresh button depends on the `/api/refresh` and `/api/refresh/status` endpoints that only exist on `portal_server.py`.

## How the Dashboard Refresh Works

When the user clicks `Refresh Data`:

- the modal opens
- the portal server starts a background collector run
- logs stream into the modal
- `fleet_data.json` is rewritten on success
- the dashboard reloads the latest JSON automatically

The header also shows:

- the active source indicator
- a link to the active `fleet_data.json`
- the generation timestamp in UTC

## Customer Grouping Logic

By default, customer grouping is tenancy-first.

This keeps the dashboard aligned to the current single-customer, single-tenancy operating model.

If you explicitly switch to `--customer-strategy tag`, the collector checks these freeform or defined tag keys in order:

- `Customer`
- `customer`
- `CustomerName`
- `customer_name`
- `Customer_Name`

If none are present, it falls back to the compartment name.

You can change grouping behavior with:

```bash
python3 build_fleet_data.py --customer-strategy tag --output fleet_data.json
python3 build_fleet_data.py --customer-strategy compartment --output fleet_data.json
python3 build_fleet_data.py --customer-strategy tenancy --output fleet_data.json
```

## Multi-Region Behavior

The collector does not rely on the single region in the OCI config file for scope.

Instead, it:

- authenticates using the configured region
- calls the Identity service to list subscribed regions
- scans each region with status `READY`

This means one valid region in `~/.oci/config` is enough.

## Current Output Schema

The generated `fleet_data.json` includes:

- `generatedAt`
- `generatedAtEpochMs`
- `schemaVersion`
- `source`
- `customers`
- `instances`

Each instance may include fields such as:

- `ID`
- `Display_Name`
- `State`
- `Availability_Domain`
- `Fault_Domain`
- `Compartment_Name`
- `Region`
- `Maintenance_UTC`
- `Last_Reboot_UTC`
- `Reboot_Evidence`
- `reboot_status`
- `Status_Change_Signal`
- `Status_Changed_UTC`

## Recommended Customer Handoff Flow

If you want to give this to a customer with minimal support overhead:

1. deliver the project folder
2. have them install Python and the OCI SDK
3. have them create `~/.oci/config`
4. have them test `build_fleet_data.py`
5. have them start `portal_server.py`
6. have them open `http://127.0.0.1:8765/index.html`

For customers with stricter security or operational requirements:

- run the collector on an OCI-hosted system
- store the JSON centrally
- expose only the static dashboard to end users

By default, the collector groups the snapshot using the tenancy name. If needed, you can still override that behavior with `--customer-strategy compartment` or `--customer-strategy tag`.

## Customer Admin Quick Start

This section is intended for customer administrators who want a concise runbook.

### Step 1: Install Python Dependency

```bash
python3 -m pip install oci
```

### Step 2: Configure OCI API Access

- create `~/.oci/config`
- place the private key at the path referenced by `key_file`
- validate that the user and API key are active in OCI

### Step 3: Build Initial Fleet Snapshot

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
```

### Step 4: Start the Portal

```bash
python3 portal_server.py --profile DEFAULT
```

### Step 5: Open the Dashboard

```text
http://127.0.0.1:8765/index.html
```

### Step 6: Run a Live Refresh

- click `Refresh Data`
- watch the modal log
- confirm the header timestamp updates after completion

## Automation Examples

### macOS or Linux Cron

Refresh every 30 minutes:

```cron
*/30 * * * * cd "/path/to/OCI Tenancy Explorer" && /usr/bin/python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json >> refresh.log 2>&1
```

### launchd or systemd

For a customer production deployment, use a service manager to:

- keep `portal_server.py` running
- rotate logs
- restart on reboot

## Troubleshooting

### The UI says `Unable to reach the local refresh endpoint`

Causes:

- the dashboard is not opened through `portal_server.py`
- port `8765` is not serving the app
- an older local server is being used

Fix:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 portal_server.py --profile DEFAULT
```

Then open:

```text
http://127.0.0.1:8765/index.html
```

### `fleet_data.json` looks stale

Causes:

- browser caching
- a different `fleet_data.json` file is being inspected
- the JSON tab was opened before refresh completed

Fixes:

- hard refresh with `Cmd+Shift+R`
- reopen `http://127.0.0.1:8765/fleet_data.json`
- confirm you are checking the file in the correct project folder

### One region hangs or times out

The collector includes timeout safeguards for:

- OS Management Hub lookups
- compute instance listing

If a region is slow, the refresh log will show:

- the target endpoint
- the compartment being queried
- whether the request completed or timed out

Common causes:

- corporate VPN interference
- local firewall or proxy behavior
- regional network path issues from the user laptop

This project was specifically validated against a case where Cisco AnyConnect caused a remote region to stall. Disconnecting VPN restored normal multi-region behavior.

### The collector only finds one region

Check:

- the tenancy is actually subscribed to the region
- the Identity call returns the region as `READY`
- the local environment can reach the regional OCI endpoint

CLI validation example:

```bash
oci iam region-subscription list --tenancy-id <TENANCY_OCID> --all
```

### The collector fails with config errors

Typical causes:

- bad `key_file` path
- incorrect OCID values
- wrong fingerprint
- wrong profile name

Common validation step:

```bash
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
```

### The portal server says address already in use

That usually means an existing portal server is already running on port `8765`.

You can simply reopen:

```text
http://127.0.0.1:8765/index.html
```

## CLI Validation Commands

These are useful when proving whether a problem is in OCI, the network path, or the local collector.

List region subscriptions:

```bash
oci iam region-subscription list --tenancy-id <TENANCY_OCID> --all
```

List compute instances in a specific region and compartment:

```bash
oci compute instance list \
  --compartment-id <COMPARTMENT_OCID> \
  --region <REGION_NAME> \
  --all \
  --debug \
  --connection-timeout 10 \
  --read-timeout 30
```

List OS Management Hub managed instances:

```bash
oci os-management-hub managed-instance list \
  --compartment-id <COMPARTMENT_OCID> \
  --region <REGION_NAME> \
  --all \
  --debug \
  --connection-timeout 10 \
  --read-timeout 30
```

## Security Notes

- do not commit `~/.oci/config`
- do not commit private API keys
- prefer instance principals where possible for server-side deployments
- limit IAM policies to read-only scope where practical
- if sharing this with customers, document whether they should deploy locally or inside OCI
- `fleet_data.json` should remain local and is intentionally git-ignored in this repo
- use `fleet_data.sample.json` for safe screenshots, docs, or demo payloads
- review `SECURITY.md` before pushing or sharing the repository with teammates

## Tenancy Metadata Exposure And Security Guidance

This application is intentionally operationally useful, which also means it exposes sensitive tenancy inventory and maintenance metadata to anyone who can open the dashboard or the raw JSON file.

The generated `fleet_data.json` may include:

- instance OCIDs
- tenancy OCIDs
- instance display names
- compartment names
- regions, availability domains, and fault domains
- maintenance schedule dates and maintenance event states
- reboot evidence and last-seen timestamps
- live migration preference metadata

Treat both the dashboard and `fleet_data.json` as sensitive internal operational data.

Recommended controls:

- host the portal only on trusted internal networks or behind an authenticated reverse proxy
- restrict filesystem access to the project directory and generated JSON files
- do not expose `fleet_data.json` on a public endpoint
- do not email or commit exported CSVs or live fleet snapshots
- use least-privilege IAM for the identity running the collector
- prefer instance principals for shared server deployments so API keys are not copied between administrators
- sanitize screenshots before sharing externally because OCIDs, compartment names, and maintenance dates can reveal internal topology
- decide whether end users really need direct access to `View Source JSON`; if not, place the portal behind network controls or proxy rules that limit who can reach it

If a customer wants broader viewer access but tighter control of tenancy metadata, the safest pattern is:

1. run the collector on a controlled backend host
2. publish the dashboard only to approved users
3. place the portal behind internal authentication
4. review whether direct raw JSON access should remain enabled

## Known Operational Notes

- `fleet_data.json` is the live source of truth for the SPA
- the browser keeps fleet data in memory for the active page session and does not persist it in IndexedDB
- the app refresh flow works only through `portal_server.py`
- the current reboot evidence is strongest when OSMH data is available
- OSMH returning zero managed instances is valid and does not mean compute data is missing

## Suggested Production Pattern

For a customer environment where multiple users need access:

1. run the collector on an OCI-hosted system or approved admin workstation
2. write `fleet_data.json` on a schedule
3. serve the dashboard and JSON from a small controlled web host
4. keep credentials or instance-principal access only on the server side

This preserves the simple architecture while making customer operations more predictable.

## Customer Deliverable Checklist

Before handing this project to a customer, confirm:

- `README.md` is included
- `oci_config.example` is included
- the project folder contains the current `index.html`, `build_fleet_data.py`, and `portal_server.py`
- the customer knows the correct launch URL is `http://127.0.0.1:8765/index.html`
- the customer understands the difference between the generated `fleet_data.json` and any legacy sample JSON files
- the customer knows whether you expect them to deploy locally or on an OCI-hosted system
- the customer has a validated OCI auth method
- the customer has tested at least one manual refresh successfully

## Change Management Notes

If you customize the application for a customer, the safest areas to tailor are:

- visual branding in `index.html`
- customer grouping strategy in `build_fleet_data.py`
- automation and hosting model
- the operational documentation modal in the UI

## Support Checklist

When a customer reports an issue, ask for:

- the exact URL they opened
- whether they used `portal_server.py`
- the full refresh console log
- whether VPN or proxy software was active
- the output of `oci iam region-subscription list`
- the output of one OCI CLI compute test in the affected region

## Quick Start Summary

If you want the shortest path to a working deployment:

```bash
cd "/path/to/OCI Tenancy Explorer"
python3 -m pip install oci
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
python3 portal_server.py --profile DEFAULT
```

Open:

```text
http://127.0.0.1:8765/index.html
```

Click:

```text
Refresh Data
```

## License and Internal Use

This repository currently appears to be a customer project/workspace artifact rather than a packaged public product. Add your preferred customer-facing license or internal distribution note here before external handoff.

ORACLE AND ITS AFFILIATES DO NOT PROVIDE ANY WARRANTY WHATSOEVER, EXPRESS OR IMPLIED, FOR ANY SOFTWARE, MATERIAL OR CONTENT OF ANY KIND CONTAINED OR PRODUCED WITHIN THIS REPOSITORY, AND IN PARTICULAR SPECIFICALLY DISCLAIM ANY AND ALL IMPLIED WARRANTIES OF TITLE, NON-INFRINGEMENT, MERCHANTABILITY, AND FITNESS FOR A PARTICULAR PURPOSE. FURTHERMORE, ORACLE AND ITS AFFILIATES DO NOT REPRESENT THAT ANY CUSTOMARY SECURITY REVIEW HAS BEEN PERFORMED WITH RESPECT TO ANY SOFTWARE, MATERIAL OR CONTENT CONTAINED OR PRODUCED WITHIN THIS REPOSITORY. IN ADDITION, AND WITHOUT LIMITING THE FOREGOING, THIRD PARTIES MAY HAVE POSTED SOFTWARE, MATERIAL OR CONTENT TO THIS REPOSITORY WITHOUT ANY REVIEW. USE AT YOUR OWN RISK.
