#!/usr/bin/env bash
set -euo pipefail

version_feature="${VERSION_FEATURE_SET:-v1}"
connector_selection="${CONNECTORS:-full}"
identity_selection="${IDENTITY_PROVIDERS-logto}"
features=("release" "${version_feature}" "redis-rs")

append_csv_features() {
  local prefix="$1"
  local values="$2"
  local value
  [[ -z "${values//[[:space:]]/}" ]] && return 0
  IFS=',' read -r -a entries <<< "${values}"
  for value in "${entries[@]}"; do
    value="${value//[[:space:]]/}"
    [[ -z "${value}" ]] && continue
    features+=("${prefix}${value}")
  done
}

if [[ "${connector_selection}" == "full" ]]; then
  features+=("connector-full")
else
  append_csv_features "connector-" "${connector_selection}"
fi
append_csv_features "identity-" "${identity_selection}"

allowed='^(release|redis-rs|v1|v2|connector-(full|adyen|airwallex|checkout|paypal|stripe)|identity-(logto|rauthy))$'
for feature in "${features[@]}"; do
  if [[ ! "${feature}" =~ ${allowed} ]]; then
    echo "Unsupported build feature: ${feature}" >&2
    exit 2
  fi
done

IFS=,
echo "${features[*]}"
