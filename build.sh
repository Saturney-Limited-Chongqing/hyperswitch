#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
features="$("${repo_dir}/scripts/resolve_build_features.sh")"
profile="${CARGO_BUILD_PROFILE:-release-fast}"

echo "Building router with features: ${features}"
exec cargo build \
  --manifest-path "${repo_dir}/Cargo.toml" \
  --package router \
  --profile "${profile}" \
  --no-default-features \
  --features "${features}"
