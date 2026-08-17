"""The publish step — the last hop, where a built board actually reaches the branch.

This is the failure that is invisible from the outside: the pipeline scrapes, appraises
and renders correctly, the job reports a red X somewhere near the end, and the site just
quietly serves yesterday's board. It happened for real on 2026-08-06, when a scheduled
run and a manual run landed within three minutes of each other and the second one lost
its whole board to a rebase conflict.

The old publish loop was ``git pull --rebase && git push``, retried three times. Against
generated output that cannot work: the rebase conflicts on every regenerated file, and
the conflict leaves the repo *mid-rebase*, so attempts two and three don't retry anything
— they die instantly on "Pulling is not possible because you have unmerged files".

These tests drive the real script against real repositories, because the bug lived
entirely in git's behaviour and no amount of mocking would have shown it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "publish.sh"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")


def _publish(repo: Path, message: str, *paths: str) -> subprocess.CompletedProcess:
    """Run the publish helper exactly as a workflow step does."""
    env = {
        **os.environ,
        "GITHUB_REF_NAME": "main",
        "PUBLISH_RETRY_SLEEP": "0",  # the backoff is real; waiting for it in tests isn't
    }
    return subprocess.run(
        ["bash", str(SCRIPT), message, *paths],
        cwd=repo, capture_output=True, text=True, env=env,
    )


@pytest.fixture
def world(tmp_path: Path):
    """A bare origin plus two clones: the run doing the publishing, and a rival run."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(origin), str(seed))
    _identify(seed)
    (seed / "docs").mkdir()
    (seed / "docs" / "index.html").write_text("<h1>original board</h1>\n")
    (seed / "src.py").write_text("original source\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "push", "--quiet", "origin", "main")

    def clone(name: str) -> Path:
        path = tmp_path / name
        _git(tmp_path, "clone", "--quiet", str(origin), str(path))
        _identify(path)
        return path

    return clone("run"), clone("rival"), origin


def _published(origin: Path, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"main:{path}"], cwd=origin,
        capture_output=True, text=True, check=True,
    ).stdout


def test_an_uncontested_board_reaches_the_branch(world):
    run, _rival, origin = world
    (run / "docs" / "index.html").write_text("<h1>fresh board</h1>\n")

    done = _publish(run, "Update deal board [skip ci]", "docs")

    assert done.returncode == 0, done.stderr
    assert _published(origin, "docs/index.html") == "<h1>fresh board</h1>\n"


def test_a_board_survives_a_run_that_pushed_first(world):
    """The 2026-08-06 regression, reproduced.

    Two runs regenerate the same three files from scratch. Merging them is meaningless,
    so the freshly-built board must win outright — and, crucially, the push must *land*.
    Under the old rebase loop this exited non-zero with the board still sitting in a
    conflicted working tree.
    """
    run, rival, origin = world

    # This run builds its board...
    (run / "docs" / "index.html").write_text("<h1>fresh board</h1>\n")
    # ...but the rival run finishes first and pushes a different board to the same file.
    (rival / "docs" / "index.html").write_text("<h1>rival board</h1>\n")
    _git(rival, "add", "-A")
    _git(rival, "commit", "--quiet", "-m", "rival board")
    _git(rival, "push", "--quiet", "origin", "main")

    done = _publish(run, "Update deal board [skip ci]", "docs")

    assert done.returncode == 0, f"stdout={done.stdout}\nstderr={done.stderr}"
    assert "rebuilding on top of" in done.stdout, done.stdout
    assert _published(origin, "docs/index.html") == "<h1>fresh board</h1>\n"
    # And the repo is left clean — not parked mid-rebase, which is what made the old
    # loop's second and third attempts useless.
    assert _git(run, "status", "--porcelain") == ""
    assert not (run / ".git" / "rebase-merge").exists()
    assert not (run / ".git" / "rebase-apply").exists()


def test_rewinding_does_not_revert_someone_elses_work(world):
    """Ours-wins applies to the published paths only.

    The obvious cheap fix — reset --soft to origin and commit our whole tree — would
    quietly roll back any source change that landed while the pipeline was running.
    """
    run, rival, origin = world
    (run / "docs" / "index.html").write_text("<h1>fresh board</h1>\n")

    (rival / "src.py").write_text("important fix\n")
    (rival / "docs" / "index.html").write_text("<h1>rival board</h1>\n")
    _git(rival, "add", "-A")
    _git(rival, "commit", "--quiet", "-m", "fix plus board")
    _git(rival, "push", "--quiet", "origin", "main")

    done = _publish(run, "Update deal board [skip ci]", "docs")

    assert done.returncode == 0, done.stderr
    assert _published(origin, "docs/index.html") == "<h1>fresh board</h1>\n"
    assert _published(origin, "src.py") == "important fix\n", "a rival's fix was reverted"


def test_an_unchanged_board_is_not_committed(world):
    """Every run rewrites docs/, but most runs change nothing worth a commit."""
    run, _rival, origin = world
    before = _git(run, "rev-parse", "HEAD")

    done = _publish(run, "Update deal board [skip ci]", "docs")

    assert done.returncode == 0, done.stderr
    assert "nothing to publish" in done.stdout
    assert _git(run, "rev-parse", "HEAD") == before


def test_a_deleted_file_is_published_as_a_deletion(world):
    """``git add <path>`` alone never stages a removal, so a piece dropped from the board
    would linger in the published catalogue forever."""
    run, _rival, origin = world
    (run / "docs" / "index.html").unlink()
    (run / "docs" / "board.html").write_text("<h1>renamed board</h1>\n")

    done = _publish(run, "Update deal board [skip ci]", "docs")

    assert done.returncode == 0, done.stderr
    listed = _git(origin, "ls-tree", "--name-only", "main", "docs/")
    assert "docs/board.html" in listed
    assert "docs/index.html" not in listed


def test_it_refuses_to_run_without_paths():
    """A silent no-arg run would report success while publishing nothing at all."""
    done = subprocess.run(
        ["bash", str(SCRIPT), "message only"],
        capture_output=True, text=True,
        env={**os.environ, "GITHUB_REF_NAME": "main"},
    )
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


def test_every_workflow_publishes_through_this_script():
    """The bug was in a shell snippet copy-pasted into three workflows. Fixing one copy
    and leaving the others is exactly how it would come back."""
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for name in ("deal-board.yml", "negotiate.yml", "keepalive.yml"):
        body = (workflows / name).read_text()
        assert "scripts/publish.sh" in body, f"{name} does not use the publish helper"
        assert "pull --rebase" not in body, f"{name} still rebases generated output"
