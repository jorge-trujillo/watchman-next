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
6. If `auto_rollback` is enabled, tags each referenced image as
   `<image>:<tag>-ROLLBACK` before pulling.
7. Pulls and recreates matched projects one at a time; waits, then verifies
   the named containers are still running.
8. **Auto-rollback** (opt-in): if the updated project fails to start, the
   original image tag is moved back onto the `<image>-ROLLBACK` image and the
   project is recreated on the previous version, then verified again.
9. Failures are isolated per project. Detailed log per run in `logs/`;
   exit code is non-zero if anything failed **or** was rolled back.

## Usage

```bash
cp config.toml.example config.toml   # then edit base_dir etc.
./watchtower_next.py --dry-run       # no changes, prints planned actions
./watchtower_next.py --verbose       # live run with command output
./watchtower_next.py --config /path/to/config.toml
```

Requires Python 3.11+ (stdlib only) and rootless `podman compose`.

## Scheduled systemd task setup (cheat sheet)

The timer runs the updater daily at 04:00 (+ up to 15 min random delay), as a
**user** unit — no root needed, matching rootless podman.

**Prerequisites:** `config.toml` exists and a manual `./watchtower_next.py --dry-run`
already ran cleanly.

### Install and enable

```bash
# 1. Copy the unit files into your user systemd directory
mkdir -p ~/.config/systemd/user
cp systemd/watchtower-next.service systemd/watchtower-next.timer ~/.config/systemd/user/

# 2. Make systemd aware of them (required after any change to unit files)
systemctl --user daemon-reload

# 3. Enable + start the timer (starts now and on every boot)
systemctl --user enable --now watchtower-next.timer

# 4. Allow your user units to run while logged out (headless server)
loginctl enable-linger $USER
loginctl show-user $USER --property=Linger   # should print Linger=yes
```

If you moved the repo somewhere other than `~/git/watchtower-next`, edit the
`ExecStart=` path in `~/.config/systemd/user/watchtower-next.service` and run
`daemon-reload` again.

### Verify it is scheduled

```bash
systemctl --user list-timers watchtower-next.timer
# -> shows NEXT / LEFT / LAST / PASSED columns; NEXT should be ~04:00
```

To prove the whole chain works without waiting for 04:00, run the service once
through systemd and check its output:

```bash
systemctl --user start watchtower-next.service
systemctl --user status watchtower-next.service     # should be inactive (dead), exit 0
journalctl --user -u watchtower-next.service -n 50  # stdout of the last run
```

Each run also writes a detailed `logs/watchtower-next-YYYYMMDD-HHMMSS.log` file.

### Change the schedule

Edit `~/.config/systemd/user/watchtower-next.timer`, adjust `OnCalendar=`, then:

```bash
systemctl --user daemon-reload && systemctl --user restart watchtower-next.timer
```

`OnCalendar=` examples: `Sat *-*-* 05:00:00` (Saturdays only),
`*-*-* 04,16:00:00` (twice daily at 04:00 and 16:00).

### Disable / remove

```bash
systemctl --user disable --now watchtower-next.timer
rm ~/.config/systemd/user/watchtower-next.{service,timer}
systemctl --user daemon-reload
```

`loginctl disable-linger $USER` if you no longer run user units at all.

### Troubleshooting

- **Timer listed but never fires**: check linger (step 4 above) and that the
  machine is on at the scheduled time — `Persistent=true` catches up a missed
  run at the next boot.
- **Service fails**: `journalctl --user -u watchtower-next.service` shows why
  (most often a missing/invalid `config.toml` — remember it defaults to
  `config.toml` next to the script).

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
| `auto_rollback` | `false` | Tag images pre-pull; auto-restore previous version on failed start |
| `rollback_tag_cleanup` | `true` | Remove ROLLBACK tags after a verified successful update |
| `no_pull_projects` | `[]` | Projects whose images are built locally; skip `pull`, recreate only |
| `digest_check` | `true` | Skip the pull when the registry digest matches the local image (needs `skopeo`) |

## Rollback / escape hatch

### Automatic (opt-in, `auto_rollback = true`)

Before each pull the script tags the currently-used image as
`<image>:<tag>-ROLLBACK` (e.g. `mongo:7` → `mongo:7-ROLLBACK`), keeping the
previous version addressable by a stable tag that survives
`podman image prune`. If the updated project fails to start, the script moves
the original tag back onto that rollback image and runs `up -d` again — the
compose file is never modified. It then verifies the containers a second time
and reports the project as **rolled back** (exit code non-zero, so you notice).

Digest-pinned image references (`image: mongo@sha256:...`) and variable-tag
references (`image: localhost:5000/app:${IMAGE_TAG}`) are skipped: the script
resolves neither without compose's environment, so rollback tagging is not
possible for them.

By default (`rollback_tag_cleanup = true`) the ROLLBACK tag is removed once an
update is verified running, so the superseded image becomes dangling and
cleanable by `podman image prune`. Set `rollback_tag_cleanup = false` to keep
the tag until the next run re-tags it. Tags are always kept when an update
fails or is rolled back.

### Manual

The previous image stays in local storage after a pull even without the
rollback feature. If an updated project misbehaves:

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