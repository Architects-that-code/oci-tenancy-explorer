# OCI Tenancy Explorer

OCI Tenancy Explorer is a lightweight dashboard for viewing OCI compute maintenance and reboot-related signals across a tenancy.

## Purpose

This tool is intended to give operations teams and tenancy administrators a quick answer to:

`Which compute instances have reboot-related maintenance signals, and what changed since the last snapshot?`

It provides a simple, portable operational view without requiring a full backend application stack.

## API Access Scope

OCI Tenancy Explorer only uses OCI publicly accessible service APIs through the official OCI SDK, authenticated with normal OCI credentials or instance principals.

- Uses read-oriented tenancy metadata calls (for example: region subscriptions, compartments, compute instance inventory, maintenance events, and optional OS Management Hub metadata).
- Does not require private/undocumented APIs.
- Does not SSH into instances or read guest OS data directly.

The project has three main parts:

- `build_fleet_data.py`: collects OCI data and writes `fleet_data.json`
- `portal_server.py`: serves the UI and provides refresh endpoints
- `index.html`: renders the tenancy dashboard from `fleet_data.json`

## Quick Start (Direct)

1. Clone the repository:

```bash
git clone git@github.com:Architects-that-code/oci-tenancy-explorer.git
cd oci-tenancy-explorer
```

2. Install Python dependency:

```bash
python3 -m pip install oci
```

3. Build the initial snapshot:

```bash
python3 build_fleet_data.py --profile DEFAULT --output fleet_data.json
```

4. Start the local portal:

```bash
python3 portal_server.py --profile DEFAULT
```

5. Open:

```text
http://127.0.0.1:8765/index.html
```

Notes:
- Configure OCI auth via `~/.oci/config` (see `oci_config.example`) or use `--auth instance_principal` when running on OCI compute.
- Keep `fleet_data.json` local; it contains tenancy metadata.

## Quick Start (Docker)

1. Clone the repository:

```bash
git clone git@github.com:Architects-that-code/oci-tenancy-explorer.git
cd oci-tenancy-explorer
```

2. Build and run:

```bash
docker build -t oci-tenancy-explorer .
docker run --rm -p 8765:8765 \
  -e OCI_AUTH=config \
  -e OCI_PROFILE=DEFAULT \
  -e OCI_CONFIG_FILE=/home/appuser/.oci/config \
  -v ~/.oci:/home/appuser/.oci:ro \
  -v ~/.ssh:/home/appuser/.ssh:ro \
  oci-tenancy-explorer
```

3. Open:

```text
http://127.0.0.1:8765/index.html
```

4. Docker Compose:

```bash
docker compose up --build
```

5. For OCI instance principal on an OCI host:

```bash
docker compose -f docker-compose.yml -f docker-compose.oci-instance-principal.yml up --build
```

## Disclaimer (Sample Code)

ORACLE AND ITS AFFILIATES DO NOT PROVIDE ANY WARRANTY WHATSOEVER, EXPRESS OR IMPLIED, FOR ANY SOFTWARE, MATERIAL OR CONTENT OF ANY KIND CONTAINED OR PRODUCED WITHIN THIS REPOSITORY, AND IN PARTICULAR SPECIFICALLY DISCLAIM ANY AND ALL IMPLIED WARRANTIES OF TITLE, NON-INFRINGEMENT, MERCHANTABILITY, AND FITNESS FOR A PARTICULAR PURPOSE. FURTHERMORE, ORACLE AND ITS AFFILIATES DO NOT REPRESENT THAT ANY CUSTOMARY SECURITY REVIEW HAS BEEN PERFORMED WITH RESPECT TO ANY SOFTWARE, MATERIAL OR CONTENT CONTAINED OR PRODUCED WITHIN THIS REPOSITORY. IN ADDITION, AND WITHOUT LIMITING THE FOREGOING, THIRD PARTIES MAY HAVE POSTED SOFTWARE, MATERIAL OR CONTENT TO THIS REPOSITORY WITHOUT ANY REVIEW. USE AT YOUR OWN RISK.
