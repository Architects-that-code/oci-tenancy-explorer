# Repository Security Notes

This project is intended to be stored in a private repository.

## Never Commit These Files

- `fleet_data.json`
- `fleet_data.local.json`
- `*.csv`
- `*.log`
- `refresh.log`
- `reboot_tracker.zip`
- `~/.oci/config`
- OCI private keys such as `*.pem` or `*.key`
- temporary tokens, local `.env` files, or any copied console output that includes OCIDs tied to a customer tenancy

## Safe Files To Share

- `oci_config.example`
- `fleet_data.sample.json`
- `README.md`
- source files such as `build_fleet_data.py`, `portal_server.py`, and `index.html`

## Team Rules

- Use placeholder OCIDs in documentation and examples.
- Keep customer exports and generated fleet snapshots local only.
- If you need sample data for a demo or screenshot, use `fleet_data.sample.json` and sanitize the customer label, OCIDs, regions, and timestamps first.
- Before every commit, review `git status` and confirm no customer or tenancy-specific data appears in tracked files.

## Recommended Check Before Pushing

Run:

```bash
git status
git diff --cached
```

If you see customer names, tenancy OCIDs, user OCIDs, exported CSV data, or private key paths that should not be shared, stop and clean the changes before committing.
