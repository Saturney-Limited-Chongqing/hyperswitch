#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend_dir="${1:-$project_root/control-center}"
revision="$(cat "$project_root/config/control_center_collection_revision")"
patch_file="$project_root/patches/control-center-collection.patch"

if [[ ! -d "$frontend_dir/.git" ]]; then
  git clone --depth 1 https://github.com/Saturney-Limited-Chongqing/hyperswitch-control-center.git "$frontend_dir"
  if [[ "$(git -C "$frontend_dir" rev-parse HEAD)" != "$revision" ]]; then
    git -C "$frontend_dir" fetch --depth 1 origin "$revision"
    git -C "$frontend_dir" checkout --detach "$revision"
  fi
fi

if [[ "$(git -C "$frontend_dir" rev-parse HEAD)" != "$revision" ]]; then
  echo "Control Center is at another revision. Use a fresh destination directory." >&2
  exit 1
fi

if git -C "$frontend_dir" apply --reverse --check "$patch_file" 2>/dev/null; then
  echo "Collection dashboard changes are already applied: $frontend_dir"
else
  git -C "$frontend_dir" apply --check "$patch_file"
  git -C "$frontend_dir" apply "$patch_file"
  echo "Collection dashboard changes applied: $frontend_dir"
fi
