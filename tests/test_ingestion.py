from pathlib import Path

from decoder.config import Tier
from decoder.ingestion.cloner import _normalize_github_url, _slug_from_url, resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def test_resolve_local_source() -> None:
    src = resolve_source(str(FIXTURE))
    assert src.kind == "local"
    assert src.path == FIXTURE.resolve()


def test_normalize_github_browser_urls() -> None:
    root = "https://github.com/owner/repo"
    # browser URLs with a path segment normalize back to the repo root
    assert _normalize_github_url(f"{root}/tree/main") == root
    assert _normalize_github_url(f"{root}/tree/main/src/utils") == root
    assert _normalize_github_url(f"{root}/blob/main/src/app.ts") == root
    assert _normalize_github_url(f"{root}/pull/42") == root
    assert _normalize_github_url(f"{root}/issues") == root
    # already-clean URLs are untouched (idempotent)
    assert _normalize_github_url(root) == root
    assert _normalize_github_url(f"{root}.git") == f"{root}.git"
    # non-github hosts are left alone
    assert _normalize_github_url("https://gitlab.com/o/r/-/tree/main") == (
        "https://gitlab.com/o/r/-/tree/main"
    )
    # the normalized URL yields the readable slug, not a sha1 hash
    assert _slug_from_url(_normalize_github_url(f"{root}/tree/main")) == "owner__repo"


def test_compute_metrics_counts_files_and_languages() -> None:
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)

    assert metrics.analyzed_files >= 5
    assert metrics.total_lines > 0
    assert "python" in metrics.languages
    assert "typescript" in metrics.languages
    assert "markdown" in metrics.languages
    assert metrics.languages["python"].files == 3
    assert metrics.languages["typescript"].files == 2


def test_decide_tier_places_sample_in_nano() -> None:
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)

    assert tier.tier is Tier.NANO
    assert "nano" in tier.reasoning.lower()
