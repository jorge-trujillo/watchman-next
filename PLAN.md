# watchtower-next — Implementation Plan

A Python replacement for watchtower: keeps podman compose projects up to date by
rebuilding from compose files (the recovery path already proven to work after
watchtower's runtime-state recreation caused permission-denied and dead-log issues).

## Goals

- Auto-update compose-managed containers **with per-project exclusions**.
- Rebuild **from compose files**, never from runtime state — avoids watchtower's
  config-drift class of failures.
- **Strict cross-reference**: only update projects whose `container_name` matches a
  currently running container.
- Simple, readable Python 3 (stdlib only, 3.11+ for `tomllib`).
- Log-file observability; failures isolated per project; systemd user timer.

## Deliverables

```
watchtower-next/
├── watchtower_next.py        # single-file CLI
├── config.toml.example       # documented sample config (copy to config.toml)
├── systemd/
│   ├── watchtower-next.service
│   └── watchtower-next.timer
├── logs/                     # runtime output (gitignored)
└── README.md
```

## Configuration (`config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `base_dir` | *(required)* | Directory whose **immediate subdirectories** are scanned (exactly 1 level: `BASE_DIR/*/compose_file`) |
| `compose_file_regex` | `compose.(yaml|yml)` | Matched **case-insensitively** against filenames — mirrors `grep -Ei "compose.(yaml|yml)"` |
| `exclude_patterns` | `[]` | Regexes matched against compose file path / project dir name; match ⇒ skip |
| `compose_command` | `podman compose` | Command used for pull/up |
| `log_dir` | `./logs` | Where dated run logs are written |
| `log_retention_days` | `30` | Older log files are deleted at start of each run |
| `post_update_wait_seconds` | `10` | Wait before verifying containers are running after update |
| `auto_rollback` | `false` | Tag images as `<image>:<tag>-ROLLBACK` pre-pull; on failed start, move the original tag back to the old image and recreate, then verify again |

## Per-run behavior

1. **Scan** `base_dir` one level deep for compose files matching the regex.
2. **Extract** `container_name` values per project via regex parsing (no PyYAML
   dependency). Missing `container_name` ⇒ project treated as unmatched.
3. **Enumerate** running containers: `podman ps --format '{{.Names}}'`.
4. **Cross-reference strictly**: update only projects with ≥1 `container_name`
   currently running. Unmatched projects logged as skipped. Running containers not
   covered by any scanned project logged informationally.
5. **Apply exclusions**.
6. **Update** each matched project: cd into its directory, then
   `podman compose pull` → `podman compose up -d --remove-orphans`
   (compose is a no-op for unchanged images, so no diffing logic needed).
7. **Verify**: after `post_update_wait_seconds`, check the project's
   `container_name`s are still running. With `auto_rollback`, a failed start
   triggers re-tagging the previous image and recreating, followed by a second
   verification. Record status:
   `updated / unchanged / rolled back / failed / skipped`.
8. **Isolate failures**: one project failing never stops the others. Final summary
   logged; exit code 0 only if no failures or rollbacks.
9. **Log**: detailed per-run file `logs/watchtower-next-YYYYMMDD-HHMMSS.log`;
   concise summary on stdout. Old logs pruned per retention setting.

## CLI

- `--config PATH` — alternate config file
- `--dry-run` — print planned actions, execute nothing
- `--verbose` — extra detail on stdout

## Scheduling

systemd **user** units (rootless podman assumed):

- `watchtower-next.timer`: daily 04:00, `RandomizedDelaySec=15m`, `Persistent=true`
- Enable: `systemctl --user enable --now watchtower-next.timer`
- Headless: `loginctl enable-linger jorge`

## README content

- Usage + config reference.
- Why recreation-from-compose avoids watchtower's permission-denied / stopped-log
  failures.
- Escape hatch: previous image is retained; manual rollback is
  `podman compose down && up -d` (optionally pinned to the old image tag).

## Verification checklist

- [ ] Dry-run scan + cross-reference prints correct projects/containers.
- [ ] Live update of one low-risk project succeeds; status logged.
- [ ] Failure isolation: forced/bogus project fails, others still update, exit code non-zero.
- [ ] Timer enabled and firing: `systemctl --user list-timers`.
- [ ] Log file written each run; retention cleanup deletes old logs.

## Non-goals (v1)

Push notifications (log-only) · image/container pruning ·
podman auto-update / quadlet integration · docker healthcheck-driven actions beyond
the running-state check.