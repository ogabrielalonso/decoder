from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from git import GitCommandError, Repo

from decoder.config import settings
from decoder.utils.logging import get_logger

logger = get_logger(__name__)

_GITHUB_URL = re.compile(r"^(https?://|git@)([\w.-]+)[:/]([\w.-]+)/([\w.-]+?)(\.git)?/?$")

# A copied GitHub browser URL carries a path segment the git remote doesn't
# expose (/tree/<branch>, /blob/<path>, /pull/<n>, ...). Strip it back to the
# repo root so the clone succeeds and the slug stays readable (owner__repo).
_GH_BROWSER = re.compile(
    r"^(https?://github\.com/[\w.-]+/[\w.-]+?)(?:\.git)?"
    r"/(?:tree|blob|commit|commits|pull|pulls|issues|releases|wiki|actions|blame|raw|branches)\b.*$",
    re.IGNORECASE,
)


def _normalize_github_url(url: str) -> str:
    """Reduce a GitHub browser URL to its cloneable repo-root form (idempotent)."""
    m = _GH_BROWSER.match(url)
    return m.group(1) if m else url


@dataclass(slots=True)
class Source:
    kind: str  # "git" or "local"
    path: Path
    origin_url: str | None
    commit: str | None
    branch: str | None
    cleanup: bool


def _slug_from_url(url: str) -> str:
    match = _GITHUB_URL.match(url)
    if match:
        owner, name = match.group(3), match.group(4)
        return f"{owner}__{name}"
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def resolve_source(source: str, *, cache: bool = True) -> Source:
    """Normalize a source reference to a local path.

    - URL -> clone into workspace/<slug>/
    - local path -> validate and return as-is
    """
    if _GITHUB_URL.match(source) or source.startswith(("http://", "https://", "git@")):
        return _clone(_normalize_github_url(source), cache=cache)

    local = Path(source).expanduser().resolve()
    if not local.exists():
        raise FileNotFoundError(f"Local source does not exist: {local}")
    if not local.is_dir():
        raise NotADirectoryError(f"Local source must be a directory: {local}")

    origin, commit, branch = _inspect_git(local)
    return Source(
        kind="local",
        path=local,
        origin_url=origin,
        commit=commit,
        branch=branch,
        cleanup=False,
    )


def _clone(url: str, *, cache: bool) -> Source:
    slug = _slug_from_url(url)
    target = (settings.workspace_dir / slug).resolve()

    if target.exists() and cache:
        logger.info("reusing cached clone at %s", target)
        commit, branch = _read_head(target)
        return Source(
            kind="git",
            path=target,
            origin_url=url,
            commit=commit,
            branch=branch,
            cleanup=False,
        )

    if target.exists():
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("cloning %s -> %s", url, target)
    try:
        Repo.clone_from(url, target, multi_options=["--filter=blob:none"])
    except GitCommandError as exc:
        raise RuntimeError(f"git clone failed for {url}: {exc}") from exc

    commit, branch = _read_head(target)
    return Source(
        kind="git",
        path=target,
        origin_url=url,
        commit=commit,
        branch=branch,
        cleanup=False,
    )


def _inspect_git(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        repo = Repo(path)
    except Exception:
        return None, None, None
    origin = None
    if repo.remotes:
        try:
            origin = next(iter(repo.remotes[0].urls), None)
        except Exception:
            origin = None
    commit = repo.head.commit.hexsha
    branch = None if repo.head.is_detached else repo.active_branch.name
    return origin, commit, branch


def _read_head(path: Path) -> tuple[str | None, str | None]:
    try:
        repo = Repo(path)
        commit = repo.head.commit.hexsha
        branch = None if repo.head.is_detached else repo.active_branch.name
        return commit, branch
    except Exception:
        return None, None
