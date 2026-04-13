# systemd Reference

This folder is documentation only.

These files are reference copies of the `systemd` units used for the SQLite
retention job. The authoritative live files on the home server are under:

- `/etc/systemd/system/weatherstation-db-retention.service`
- `/etc/systemd/system/weatherstation-db-retention.timer`

The active environment file on the server is:

- `/etc/weatherstation-home.env`

The current reference units in this folder are configured to:

- run `weatherstation-db-retention.service` daily at `03:17` local time
- apply `RandomizedDelaySec=15m` with `Persistent=true`
- execute as user/group `gardener`
- use `/home/gardener/weatherstation-home/weatherstation` as the working directory
- run `/home/gardener/weatherstation-home/.venv/bin/python /home/gardener/weatherstation-home/weatherstation/retention.py`

Use these commands on the server to inspect the installed units:

```bash
systemctl cat weatherstation-db-retention.service
systemctl cat weatherstation-db-retention.timer
systemctl show -p FragmentPath weatherstation-db-retention.service
systemctl show -p FragmentPath weatherstation-db-retention.timer
```

Files in this folder are intended to:

- show the expected unit content in source control
- document the real server locations
- give you a stable reference when comparing repo state to the live server

If the live unit files are edited directly on the server, update these
reference copies in the repo as well so they do not drift.
