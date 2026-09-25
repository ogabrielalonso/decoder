from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from git import InvalidGitRepositoryError, Repo

from decoder.schemas import FileHistory
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


def collect_history(root: Path, *, max_commits: int = 2000) -> list[FileHistory]:
    try:
        repo = Repo(root)
    except InvalidGitRepositoryError:
        logger.info("no git repo at %s, skipping history", root)
        return []

    files: dict[str, FileHistory] = {}
    author_sets: dict[str, set[str]] = defaultdict(set)

    commits = list(repo.iter_commits(max_count=max_commits))
    for commit in commits:
        author = f"{commit.author.name} <{commit.author.email}>"
        when = datetime.fromtimestamp(commit.committed_date, tz=UTC)
        try:
            stats = commit.stats.files
        except Exception:
            continue
        for raw_path, entry in stats.items():
            rel_path = str(raw_path)
            fh = files.get(rel_path)
            if fh is None:
                fh = FileHistory(file=rel_path)
                files[rel_path] = fh
            fh.commits += 1
            fh.insertions += entry.get("insertions", 0)
            fh.deletions += entry.get("deletions", 0)
            if fh.first_commit is None or when < fh.first_commit:
                fh.first_commit = when
            if fh.last_commit is None or when > fh.last_commit:
                fh.last_commit = when
            author_sets[rel_path].add(author)

    for rel_path, authors in author_sets.items():
        files[rel_path].authors = sorted(authors)

    return list(files.values())
