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
import json
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
DIGEST_TIMEOUT_SECONDS = 120
LOG_FILE_GLOB = "watchtower-next-*.log"

CONTAINER_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([\w.-]+)[\"']?\s*(?:#.*)?$"
)
IMAGE_REF_RE = re.compile(r"^\s*image:\s*[\"']?([^\s\"'#]+)[\"']?\s*(?:#.*)?$")

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
    auto_rollback: bool = False
    rollback_tag_cleanup: bool = True
    no_pull_projects: tuple[str, ...] = ()
    digest_check: bool = True
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
    rolled_back: int = 0
    failed: int = 0
    skipped_unmatched: int = 0
    excluded: int = 0

    def summary(self) -> str:
        return (
            f"{self.updated} updated, {self.unchanged} unchanged, "
            f"{self.rolled_back} rolled back, {self.failed} failed, "
            f"{self.skipped_unmatched} skipped (no running match), "
            f"{self.excluded} excluded"
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
    auto_rollback = bool(raw.get("auto_rollback", False))
    rollback_tag_cleanup = bool(raw.get("rollback_tag_cleanup", True))
    no_pull_projects = tuple(str(p) for p in raw.get("no_pull_projects", []))
    digest_check = bool(raw.get("digest_check", True))

    return Config(
        base_dir=base_dir,
        file_regex=file_regex,
        exclude_patterns=exclude_patterns,
        compose_command=compose_command,
        log_dir=log_dir,
        log_retention_days=log_retention_days,
        post_update_wait_seconds=post_update_wait_seconds,
        auto_rollback=auto_rollback,
        rollback_tag_cleanup=rollback_tag_cleanup,
        no_pull_projects=no_pull_projects,
        digest_check=digest_check,
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


def extract_image_refs(compose_file: Path) -> list[str]:
    """Pull unique ``image:`` references out of a compose file."""
    refs: list[str] = []
    text = compose_file.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = IMAGE_REF_RE.match(line)
        if match and match.group(1) not in refs:
            refs.append(match.group(1))
    return refs


def split_image_ref(ref: str) -> tuple[str, str] | None:
    """Split an image reference into ``(base, tag)``.

    Returns ``None`` for digest-pinned references: a digest cannot drift on
    pull, so those images never need a rollback tag.
    """
    if "@" in ref:
        return None
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :]
    return ref, "latest"


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


# --------------------------------------------------------------------------
# Remote digest check
# --------------------------------------------------------------------------


def remote_image_digest(ref: str) -> str | None:
    """Current digest of *ref* in its registry, via skopeo. ``None`` if unknown.

    A read-only registry query (manifest only, no layers). ``None`` means the
    check could not run or be trusted, and callers should fall back to pulling.
    """
    try:
        result = subprocess.run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{ref}"],
            capture_output=True,
            text=True,
            timeout=DIGEST_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("skopeo inspect unavailable for %s: %s", ref, exc)
        return None
    if result.returncode != 0:
        log.debug("skopeo inspect failed for %s", ref)
        return None
    digest = result.stdout.strip()
    return digest if digest.startswith("sha256:") else None


def local_image_digests(ref: str) -> set[str]:
    """Digest values recorded in the local image's ``RepoDigests``."""
    try:
        result = subprocess.run(
            ["podman", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return set()
        entries = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return set()
    return {entry.split("@", 1)[1] for entry in entries if "@" in entry}


def images_are_current(project: Project, refs: list[str]) -> bool:
    """True when every checkable *ref* already matches its registry digest.

    Refs this script cannot resolve (``${VAR}`` tags) force a pull; refs whose
    digest cannot be fetched also force a pull, so a broken skopeo setup
    degrades to the old always-pull behavior rather than skipping updates.
    """
    for ref in refs:
        if "${" in ref:
            return False
        remote = remote_image_digest(ref)
        if remote is None:
            return False
        if remote not in local_image_digests(ref):
            log.info(
                "[%s] %s changed upstream (registry digest %s)",
                project.name,
                ref,
                remote,
            )
            return False
    return True


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


# --------------------------------------------------------------------------
# Automatic rollback
# --------------------------------------------------------------------------


def rollback_ref_for(ref: str) -> str | None:
    """Return ``<image>:<tag>-ROLLBACK`` for a tag-based image reference.

    Returns ``None`` for digest-pinned and variable-tag references: a digest
    cannot drift on pull, and a ``${VAR}`` tag cannot be resolved without
    compose's environment, so neither gets a rollback tag.
    """
    if "@" in ref or "${" in ref:
        return None
    base, tag = split_image_ref(ref)
    return f"{base}:{tag}-ROLLBACK"


def podman_image_exists(ref: str) -> bool:
    result = subprocess.run(
        ["podman", "image", "exists", ref], capture_output=True, timeout=60
    )
    return result.returncode == 0


def podman_tag(source: str, target: str) -> None:
    result = subprocess.run(
        ["podman", "tag", source, target], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"podman tag {source} -> {target} failed: {result.stderr.strip()}"
        )


def tag_rollback_targets(config: Config, project: Project) -> None:
    """Tag each local image referenced by the project as ``<image>-ROLLBACK``.

    Called before pulling so the previous version stays addressable by a
    stable, human-readable tag (and survives ``podman image prune``).
    In dry-run mode the planned tags are only logged.
    """
    for ref in extract_image_refs(project.compose_file):
        rollback_ref = rollback_ref_for(ref)
        if rollback_ref is None:
            if "${" in ref:
                log.info(
                    "[%s] %s uses a variable tag; skipping rollback tag "
                    "(rollback unavailable for this project)",
                    project.name,
                    ref,
                )
            else:
                log.debug(
                    "[%s] %s is digest-pinned; no rollback tag needed",
                    project.name,
                    ref,
                )
            continue
        try:
            if not podman_image_exists(ref):
                log.warning(
                    "[%s] image %s not present locally; no rollback tag created",
                    project.name,
                    ref,
                )
                continue
            if config.dry_run:
                log.info(
                    "[%s] would tag rollback image %s -> %s",
                    project.name,
                    ref,
                    rollback_ref,
                )
                continue
            podman_tag(ref, rollback_ref)
            log.debug("[%s] tagged rollback image %s", project.name, rollback_ref)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            log.warning(
                "[%s] could not tag rollback image for %s: %s", project.name, ref, exc
            )


def cleanup_rollback_tags(config: Config, project: Project) -> None:
    """Remove ``<image>-ROLLBACK`` tags after a verified successful update.

    Untagging lets ``podman image prune`` reclaim the superseded image. Best
    effort: failures are logged but do not fail the update.
    """
    for ref in extract_image_refs(project.compose_file):
        rollback_ref = rollback_ref_for(ref)
        if rollback_ref is None:
            continue
        try:
            if not podman_image_exists(rollback_ref):
                continue
            if config.dry_run:
                log.info(
                    "[%s] would remove rollback tag %s",
                    project.name,
                    rollback_ref,
                )
                continue
            result = subprocess.run(
                ["podman", "rmi", rollback_ref],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log.warning(
                    "[%s] could not remove rollback tag %s: %s",
                    project.name,
                    rollback_ref,
                    result.stderr.strip(),
                )
                continue
            log.debug("[%s] removed rollback tag %s", project.name, rollback_ref)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            log.warning(
                "[%s] could not remove rollback tag for %s: %s",
                project.name,
                ref,
                exc,
            )


def attempt_rollback(config: Config, project: Project) -> bool:
    """Restore the previous image tags and recreate the project. Best effort.

    Moving the original tag back onto the old image makes the untouched
    compose file resolve to the previous version on the next ``up -d``.
    """
    try:
        for ref in extract_image_refs(project.compose_file):
            rollback_ref = rollback_ref_for(ref)
            if rollback_ref is None:
                continue
            if not podman_image_exists(rollback_ref):
                log.warning(
                    "[%s] no rollback image %s; leaving %s as-is",
                    project.name,
                    rollback_ref,
                    ref,
                )
                continue
            podman_tag(rollback_ref, ref)
            log.warning("[%s] restored %s from %s", project.name, ref, rollback_ref)
        recreate_containers(config, project)
        return True
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        log.error("[%s] rollback failed: %s", project.name, exc)
        return False


def handle_failed_start(config: Config, project: Project, missing: list[str]) -> str:
    """Handle an update that left containers down. Returns the final status."""
    if not config.auto_rollback:
        log.error(
            "[%s] FAILED verification after update, not running: %s",
            project.name,
            ", ".join(missing),
        )
        return "failed"
    log.warning(
        "[%s] containers not running after update: %s; rolling back",
        project.name,
        ", ".join(missing),
    )
    if not attempt_rollback(config, project):
        return "failed"
    time.sleep(config.post_update_wait_seconds)
    still_missing = verify_running(project)
    if still_missing:
        log.error(
            "[%s] rollback did not restore containers: %s",
            project.name,
            ", ".join(still_missing),
        )
        return "failed"
    log.warning(
        "[%s] rolled back to previous image(s); update did not stick", project.name
    )
    return "rolled_back"


def update_project(config: Config, project: Project) -> str:
    """Update one project. Returns ``updated`` or ``unchanged``.

    Projects listed in ``no_pull_projects`` skip the pull step: their images
    are built locally and only change when rebuilt, in which case ``up -d``
    recreates the containers from the new image ID.
    """
    before = snapshot_containers(project, list_running_containers())
    if project.name in config.no_pull_projects:
        log.info("[%s] no_pull project; skipping pull", project.name)
    elif config.digest_check and images_are_current(
        project, extract_image_refs(project.compose_file)
    ):
        log.info("[%s] images already current; skipping pull", project.name)
    else:
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


def check_compose_provider(config: Config) -> None:
    """Verify the compose command can locate a provider before touching anything.

    ``podman compose`` is only a dispatcher; without docker-compose or
    podman-compose on the PATH every pull/up fails with exit 125. Run the
    cheap read-only ``version`` subcommand first so a broken environment
    aborts the whole run instead of failing project by project.
    """
    cmd = [*config.compose_command, "version"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(
            f"could not run {shlex.join(cmd)}: {exc}"
        ) from exc
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(
            f"{shlex.join(cmd)} failed with exit code {result.returncode}: {output} "
            "(is a compose provider such as podman-compose or docker-compose "
            "installed and on the PATH of the user running this script?)"
        )
    log.debug("compose provider check passed: %s", shlex.join(cmd))


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
        check_compose_provider(config)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        log.error("compose provider check failed: %s", exc)
        return EXIT_SETUP_ERROR

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
            if config.auto_rollback:
                tag_rollback_targets(config, project)
            status = update_project(config, project)
            if status == "updated" and not config.dry_run:
                time.sleep(config.post_update_wait_seconds)
                missing = verify_running(project)
                if missing:
                    status = handle_failed_start(config, project, missing)
                elif config.rollback_tag_cleanup:
                    cleanup_rollback_tags(config, project)
            if status in ("failed", "rolled_back"):
                if status == "failed":
                    stats.failed += 1
                else:
                    stats.rolled_back += 1
            else:
                setattr(stats, status, getattr(stats, status) + 1)
                log.info(
                    "[%s] %s%s",
                    project.name,
                    status,
                    ""
                    if config.dry_run
                    else f" (containers: {', '.join(project.container_names)})",
                )
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            log.error("[%s] FAILED: %s", project.name, exc)
            status = "failed"
            if config.auto_rollback and not config.dry_run:
                log.warning("[%s] attempting rollback after failure", project.name)
                if attempt_rollback(config, project):
                    status = "rolled_back"
                    log.warning(
                        "[%s] rolled back to previous image(s)", project.name
                    )
            if status == "rolled_back":
                stats.rolled_back += 1
            else:
                stats.failed += 1

    elapsed = datetime.now() - start
    log.info("run finished in %s: %s", elapsed, stats.summary())
    return EXIT_FAILURES if (stats.failed or stats.rolled_back) else EXIT_OK


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