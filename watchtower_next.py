#!/usr/bin/env python3
"""watchtower-next: keep podman compose projects up to date.

A compose-file-based replacement for watchtower. Projects are pulled and
recreated from their compose files, avoiding the runtime-state drift that
caused permission errors and broken logging under watchtower.

Behavior:
  * Scans one level of subdirectories under ``base_dir`` for compose files
    whose names match a case-insensitive regex.
  * Extracts ``container_name`` values from each compose file.
  * Updates only projects with at least one currently running container.
  * Per project: ``<compose_command> pull`` then ``up -d --remove-orphans``.
  * Failures are isolated per project; a summary and exit code report them.
"""

from __future__ import annotations

import argparse
import logging
import re
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.toml"
DEFAULT_FILE_REGEX = r"compose.(yaml|yml)"
DEFAULT_LOG_RETENTION_DAYS = 30
DEFAULT_POST_UPDATE_WAIT_SECONDS = 10
PULL_TIMEOUT_SECONDS = 1800
UP_TIMEOUT_SECONDS = 600
LOG_FILE_GLOB = "watchtower-next-*.log"

CONTAINER_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([\w.-]+)[\"']?\s*(?:#.*)?$"
)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_SETUP_ERROR = 2

log = logging.getLogger("watchtower-next")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    base_dir: Path
    file_regex: re.Pattern[str]
    exclude_patterns: tuple[re.Pattern[str], ...]
    compose_command: tuple[str, ...]
    log_dir: Path
    log_retention_days: int
    post_update_wait_seconds: int
    dry_run: bool = False


@dataclass
class Project:
    name: str
    compose_file: Path
    container_names: list[str] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        return self.compose_file.parent


@dataclass
class RunStats:
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped_unmatched: int = 0
    excluded: int = 0

    def summary(self) -> str:
        return (
            f"{self.updated} updated, {self.unchanged} unchanged, "
            f"{self.failed} failed, {self.skipped_unmatched} skipped "
            f"(no running match), {self.excluded} excluded"
        )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _resolve_path(config_file: Path, raw: str) -> Path:
    """Resolve *raw* relative to the config file's directory."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_file.parent / path
    return path


def load_config(config_file: Path, dry_run: bool) -> Config:
    if not config_file.is_file():
        raise ConfigError(f"config file not found: {config_file}")
    try:
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_file}: {exc}") from exc

    base_dir_raw = raw.get("base_dir")
    if not base_dir_raw:
        raise ConfigError("base_dir is required")
    base_dir = _resolve_path(config_file, str(base_dir_raw))
    if not base_dir.is_dir():
        raise ConfigError(f"base_dir is not a directory: {base_dir}")

    try:
        file_regex = re.compile(
            str(raw.get("compose_file_regex", DEFAULT_FILE_REGEX)), re.IGNORECASE
        )
        exclude_patterns = tuple(
            re.compile(str(pattern)) for pattern in raw.get("exclude_patterns", [])
        )
    except re.error as exc:
        raise ConfigError(f"invalid regex in config: {exc}") from exc

    compose_command = tuple(shlex.split(str(raw.get("compose_command", "podman compose"))))
    if not compose_command:
        raise ConfigError("compose_command is empty")

    log_dir = _resolve_path(config_file, str(raw.get("log_dir", "logs")))
    log_retention_days = int(raw.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))
    post_update_wait_seconds = int(
        raw.get("post_update_wait_seconds", DEFAULT_POST_UPDATE_WAIT_SECONDS)
    )

    return Config(
        base_dir=base_dir,
        file_regex=file_regex,
        exclude_patterns=exclude_patterns,
        compose_command=compose_command,
        log_dir=log_dir,
        log_retention_days=log_retention_days,
        post_update_wait_seconds=post_update_wait_seconds,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------
# Scanning and cross-referencing
# --------------------------------------------------------------------------


def extract_container_names(compose_file: Path) -> list[str]:
    """Pull ``container_name`` values out of a compose file without PyYAML."""
    names: list[str] = []
    text = compose_file.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = CONTAINER_NAME_RE.match(line)
        if match:
            names.append(match.group(1))
    return names


def scan_projects(config: Config) -> list[Project]:
    """Find compose files one level under base_dir (BASE_DIR/*/file)."""
    projects: list[Project] = []
    for child in sorted(config.base_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        matches = sorted(
            f for f in child.iterdir() if f.is_file() and config.file_regex.search(f.name)
        )
        if not matches:
            continue
        if len(matches) > 1:
            log.warning(
                "project %s has multiple compose file matches, using %s (ignored: %s)",
                child.name,
                matches[0].name,
                ", ".join(m.name for m in matches[1:]),
            )
        compose_file = matches[0]
        projects.append(
            Project(
                name=child.name,
                compose_file=compose_file,
                container_names=extract_container_names(compose_file),
            )
        )
    return projects


def match_exclusion(project: Project, config: Config) -> str | None:
    """Return the pattern that excludes this project, if any."""
    for pattern in config.exclude_patterns:
        if pattern.search(project.name) or pattern.search(str(project.compose_file)):
            return pattern.pattern
    return None


def list_running_containers() -> dict[str, str]:
    """Return running containers as ``name -> id``."""
    result = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}\t{{.ID}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"podman ps failed: {result.stderr.strip()}")
    containers: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, container_id = line.partition("\t")
        if name:
            containers[name.strip()] = container_id.strip()
    return containers


# --------------------------------------------------------------------------
# Updating
# --------------------------------------------------------------------------


def _compose_args(config: Config, project: Project, *args: str) -> list[str]:
    return [*config.compose_command, "-f", str(project.compose_file), *args]


def _log_result(label: str, result: subprocess.CompletedProcess[str]) -> None:
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        log.debug("%s: %s", label, line)


def pull_images(config: Config, project: Project) -> None:
    cmd = _compose_args(config, project, "pull")
    log.info("[%s] pulling: %s", project.name, shlex.join(cmd))
    if config.dry_run:
        return
    result = subprocess.run(
        cmd, cwd=project.directory, capture_output=True, text=True,
        timeout=PULL_TIMEOUT_SECONDS,
    )
    _log_result("pull", result)
    if result.returncode != 0:
        raise RuntimeError(f"pull failed with exit code {result.returncode}")


def recreate_containers(config: Config, project: Project) -> None:
    cmd = _compose_args(config, project, "up", "-d", "--remove-orphans")
    log.info("[%s] recreating: %s", project.name, shlex.join(cmd))
    if config.dry_run:
        return
    result = subprocess.run(
        cmd, cwd=project.directory, capture_output=True, text=True,
        timeout=UP_TIMEOUT_SECONDS,
    )
    _log_result("up", result)
    if result.returncode != 0:
        raise RuntimeError(f"up failed with exit code {result.returncode}")


def snapshot_containers(
    project: Project, running: dict[str, str]
) -> dict[str, str]:
    """Container ids for this project's named containers."""
    return {n: i for n, i in running.items() if n in project.container_names}


def verify_running(project: Project) -> list[str]:
    """Return this project's container names that are not running."""
    running = list_running_containers()
    return [n for n in project.container_names if n not in running]


def update_project(config: Config, project: Project) -> str:
    """Update one project. Returns ``updated`` or ``unchanged``."""
    before = snapshot_containers(project, list_running_containers())
    pull_images(config, project)
    recreate_containers(config, project)
    if config.dry_run:
        return "updated"
    after = snapshot_containers(project, list_running_containers())
    return "updated" if after != before else "unchanged"


# --------------------------------------------------------------------------
# Logging setup and housekeeping
# --------------------------------------------------------------------------


def setup_logging(log_dir: Path, start: datetime, verbose: bool) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"watchtower-next-{start.strftime('%Y%m%d-%H%M%S')}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return log_file


def prune_old_logs(log_dir: Path, retention_days: int) -> int:
    """Delete run logs older than the retention window. Returns removed count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for log_file in log_dir.glob(LOG_FILE_GLOB):
        mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            log_file.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compose-file-based auto-updater for podman projects."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / DEFAULT_CONFIG_NAME,
        help=f"config file path (default: {DEFAULT_CONFIG_NAME} next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned actions without executing them",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="show command output on stdout"
    )
    return parser.parse_args(argv)


def classify_projects(
    config: Config, projects: list[Project], running: dict[str, str]
) -> tuple[list[Project], RunStats]:
    """Split projects into updatable and non-updatable, logging the reasons."""
    stats = RunStats()
    updatable: list[Project] = []
    covered_names: set[str] = set()

    for project in projects:
        exclusion = match_exclusion(project, config)
        if exclusion is not None:
            stats.excluded += 1
            log.info("[%s] excluded (matched %r)", project.name, exclusion)
            continue
        running_here = [n for n in project.container_names if n in running]
        if not running_here:
            stats.skipped_unmatched += 1
            log.warning(
                "[%s] skipped: no running container matches container_name (%s)",
                project.name,
                ", ".join(project.container_names) or "none defined",
            )
            continue
        covered_names.update(project.container_names)
        log.info(
            "[%s] matched running containers: %s", project.name, ", ".join(running_here)
        )
        updatable.append(project)

    uncovered = sorted(set(running) - covered_names)
    if uncovered:
        log.info(
            "running containers not covered by any scanned project: %s",
            ", ".join(uncovered),
        )
    return updatable, stats


def run(config: Config, verbose: bool = False) -> int:
    start = datetime.now()
    log_file = setup_logging(config.log_dir, start, verbose=verbose)
    log.info("watchtower-next run started (log: %s)", log_file)
    if config.dry_run:
        log.info("dry-run mode: no changes will be made")

    if not config.dry_run:
        removed = prune_old_logs(config.log_dir, config.log_retention_days)
        if removed:
            log.info("pruned %d old log file(s)", removed)

    try:
        running = list_running_containers()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        log.error("could not list running containers: %s", exc)
        return EXIT_SETUP_ERROR
    log.info("found %d running container(s)", len(running))

    projects = scan_projects(config)
    log.info(
        "scanned %d project(s) under %s", len(projects), config.base_dir
    )
    if not projects:
        log.warning("no compose files found; check base_dir and compose_file_regex")

    updatable, stats = classify_projects(config, projects, running)

    for project in updatable:
        try:
            status = update_project(config, project)
            if status == "updated" and not config.dry_run:
                time.sleep(config.post_update_wait_seconds)
                missing = verify_running(project)
                if missing:
                    stats.failed += 1
                    log.error(
                        "[%s] FAILED verification after update, not running: %s",
                        project.name,
                        ", ".join(missing),
                    )
                    continue
            setattr(stats, status, getattr(stats, status) + 1)
            log.info(
                "[%s] %s%s",
                project.name,
                status,
                "" if config.dry_run else f" (containers: {', '.join(project.container_names)})",
            )
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            stats.failed += 1
            log.error("[%s] FAILED: %s", project.name, exc)

    elapsed = datetime.now() - start
    log.info("run finished in %s: %s", elapsed, stats.summary())
    return EXIT_FAILURES if stats.failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR
    try:
        return run(config, verbose=args.verbose)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())