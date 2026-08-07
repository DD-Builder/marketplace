#!/usr/bin/env bash
#
# Publish generated output to the branch, surviving a concurrent push.
#
# Every path handed to this script holds *generated* output — the board, the negotiation
# drafts, the keepalive stamp — rebuilt from scratch by the run that calls it. That makes
# `git pull --rebase` both wrong and fatal here:
#
#   * wrong, because a regenerated file's content is not a merge of two histories. There
#     is no sense in which half of one run's board and half of another's is correct.
#   * fatal, because the first CONFLICT strands the repo mid-rebase, and every subsequent
#     retry then dies on "Pulling is not possible because you have unmerged files" — the
#     retry loop cannot make progress, it just burns its three attempts.
#
# A real run lost a freshly-built board exactly that way: two runs landed the same day,
# all three of docs/{catalog.json,index.html,status.json} conflicted, and the board was
# built and then silently thrown away.
#
# So the resolution is unconditional: if someone pushed first, rewind onto their tip and
# lay this run's output back down whole. Only the given paths are overwritten, so a
# concurrent change to anything else (source, workflows) is preserved rather than
# reverted — which a plain `reset --soft` would not manage.
#
# Usage: publish.sh <commit-message> <path> [path...]

# Deliberately no `-e`: every failure below is handled explicitly, and an unexpected exit
# mid-loop is what stranded the old version.
set -uo pipefail

msg="${1:-}"
shift || true
if [ -z "$msg" ] || [ "$#" -eq 0 ]; then
  echo "usage: publish.sh <commit-message> <path> [path...]" >&2
  exit 2
fi

branch="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"
# Tests drive the retry path and have no reason to wait out the real backoff.
retry_sleep="${PUBLISH_RETRY_SLEEP:-5}"

# Snapshot the generated output before the first push; each retry restores it verbatim,
# so a rewind can never leave us publishing origin's copy of our own output.
snapshot="$(mktemp -d)"
trap 'rm -rf "$snapshot"' EXIT
for path in "$@"; do
  if [ -e "$path" ]; then
    mkdir -p "$snapshot/$(dirname "$path")"
    cp -R "$path" "$snapshot/$path"
  fi
done

for attempt in 1 2 3; do
  git add -A -- "$@"
  if git diff --staged --quiet; then
    echo "publish: nothing to publish"
    exit 0
  fi
  if ! git commit -q -m "$msg"; then
    echo "::error::publish: could not commit $*" >&2
    exit 1
  fi
  if git push -q origin "HEAD:$branch"; then
    echo "publish: pushed on attempt $attempt"
    exit 0
  fi

  echo "publish: push rejected, rebuilding on top of origin/$branch (attempt $attempt)"
  git fetch -q origin "$branch" || true
  git reset -q --hard "origin/$branch" || true
  for path in "$@"; do
    rm -rf "$path"
    if [ -e "$snapshot/$path" ]; then
      mkdir -p "$(dirname "$path")"
      cp -R "$snapshot/$path" "$path"
    fi
  done
  sleep $((attempt * retry_sleep))
done

echo "::error::publish: could not publish $* after 3 attempts" >&2
exit 1
