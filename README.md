# watchtower-next

A compose-file-based replacement for [watchtower](https://containrrr.dev/watchtower/)
for podman + docker-compose-style setups.

## Why

watchtower recreates containers from **runtime state** via the Docker-compat API
instead of rebuilding from compose files. That config drift caused:

- containers failing to restart with permission errors
- container logs stopping

`watchtower-next` instead runs, per project:

```
podman compose pull
podman compose up -d --remove-orphans
```

from the project's own directory — the same recovery path that reliably fixed
those breakages manually. Compose is a no-op for images that did not change, so
unchanged projects are untouched.

## How it works

1. Scans `base_dir` **one level deep** for compose files matching a
   case-insensitive regex (default `compose.(yaml|yml)` — also matches
   `docker-compose.yml`).
2. Extracts `container_name` values from each compose file (regex parse, no
   PyYAML dependency).
3. Lists running containers via `podman ps`.
4. **Strict cross-reference**: only projects with at least one
   `container_name` currently running are updated. Unmatched projects are
   skipped and logged; running containers not covered by any scanned project
   are reported informationally.
5. Applies `exclude_patterns` (regex against project dir name / file path).
6. Pulls and recreates matched projects one at a time; waits, then verifies
   the named containers are still running.
7. Failures are isolated per project. Detailed log per run in `logs/`;
   exit code is non-zero if anything failed.

## Usage

```bash
cp config.toml.example config.toml   # then edit base_dir etc.
./watchtower_next.py --dry-run       # no changes, prints planned actions
./watchtower_next.py --verbose       # live run with command output
./watchtower_next.py --config /path/to/config.toml
```

Requires Python 3.11+ (stdlib only) and rootless `podman compose`.

## Scheduling (systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/watchtower-next.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now watchtower-next.timer
loginctl enable-linger $USER   # run without an active login session
```

The timer runs daily at 04:00 with 15 minutes of random delay (`Persistent=true`
catches up after downtime). Check with `systemctl --user list-timers` and
`journalctl --user -u watchtower-next.service`.

## Configuration

See [config.toml.example](config.toml.example) for every option:

| Key | Default | Meaning |
|---|---|---|
| `base_dir` | *(required)* | Scan root; immediate subdirectories only |
| `compose_file_regex` | `compose.(yaml|yml)` | Case-insensitive filename regex |
| `exclude_patterns` | `[]` | Regexes to skip projects |
| `compose_command` | `podman compose` | Pull/up command prefix |
| `log_dir` | `logs` | Where dated run logs are written |
| `log_retention_days` | `30` | Old log cleanup window |
| `post_update_wait_seconds` | `10` | Wait before post-update verification |

## Rollback / escape hatch

The previous image stays in local storage after a pull. If an updated project
misbehaves:

```bash
cd ~/containers/<project>
podman compose down
# optionally pin the old image tag in the compose file
podman compose up -d
```

Note: if a **new image version itself** changes UID expectations against
bind-mounted volumes, the update will still fail — that is an image-side
change, not drift. The script isolates the failure to that one project,
leaves the old image available, and reports it in the log.

## Verification

- `--dry-run` should list the expected projects, matches, and exclusions.
- `systemctl --user list-timers` shows the next trigger.
- Each run writes `logs/watchtower-next-YYYYMMDD-HHMMSS.log`.