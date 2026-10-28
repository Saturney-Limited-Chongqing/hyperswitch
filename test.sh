#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
features="$("${repo_dir}/scripts/resolve_build_features.sh")"

echo "Checking router with features: ${features}"
if [[ "${ALL_TARGETS:-false}" == "true" ]]; then
  set -- --all-targets
else
  set --
fi
cargo check \
  --manifest-path "${repo_dir}/Cargo.toml" \
  --package router \
  "$@" \
  --no-default-features \
  --features "${features}"

(cd "${repo_dir}/scripts" && python3 -B -m unittest -q test_logto_account_sync)
