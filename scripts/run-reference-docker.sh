#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
scenario="${1:-all}"
output="${2:-$ROOT/results/robot-reference-001}"
case "$scenario" in all|navigation|perception|arm) ;; *) echo 'scenario: all/navigation/perception/arm' >&2; exit 2;; esac
if [[ -e "$output" || -L "$output" ]]; then echo "Output already exists: $output" >&2; exit 2; fi
mkdir -p "$(dirname "$output")"
parent="$(cd "$(dirname "$output")" && pwd)"
name="$(basename "$output")"
if [[ "$name" == '.' || "$name" == '..' || "$name" == *','* ]]; then echo 'Invalid output name' >&2; exit 2; fi
base="${BASE_IMAGE:-ros:humble-ros-base-jammy}"
image="${REFERENCE_IMAGE:-robot-systems-perf:reference}"
engine="$(docker info --format '{{.Architecture}}')"
case "$engine" in aarch64|arm64) arch=arm64;; x86_64|amd64) arch=amd64;; *) echo "Unsupported engine architecture: $engine" >&2; exit 2;; esac
if ! docker image inspect "$base" >/dev/null 2>&1 || [[ "$(docker image inspect "$base" --format '{{.Architecture}}')" != "$arch" ]]; then
  docker pull --platform "linux/$arch" "$base"
fi
base_arch="$(docker image inspect "$base" --format '{{.Architecture}}')"
if [[ "$base_arch" != "$arch" ]]; then echo 'Base image must match native engine architecture' >&2; exit 2; fi
base_id="$(docker image inspect "$base" --format '{{.Id}}')"
revision="$(git -C "$ROOT" rev-parse HEAD)"
if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then revision="$revision+dirty"; fi
docker build -f "$ROOT/scenarios/robot_reference/Dockerfile" \
  --build-arg "BASE_IMAGE=$base" --build-arg "BASE_IMAGE_ID=$base_id" \
  --build-arg "SOURCE_REVISION=$revision" -t "$image" "$ROOT"
image_arch="$(docker image inspect "$image" --format '{{.Architecture}}')"
if [[ "$image_arch" != "$arch" ]]; then echo 'Built image is not native' >&2; exit 2; fi
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
kind=docker-linux-reference
if [[ "$(uname -s)" != Linux ]]; then kind=docker-desktop-vm-reference; fi
docker run --rm --init --stop-timeout 15 --platform "linux/$arch" \
  --user "$(id -u):$(id -g)" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=256m --network bridge \
  --mount "type=bind,src=$parent,dst=/results" \
  -e "EP_ENVIRONMENT_KIND=$kind" -e "EP_IMAGE_ID=$image_id" \
  "$image_id" python3 -m scenarios.robot_reference.run --scenario "$scenario" \
  --output "/results/$name" "${@:3}"
