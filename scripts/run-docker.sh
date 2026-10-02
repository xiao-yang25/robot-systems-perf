#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-configs/smoke.json}"
OUTPUT="${2:-results/run-$(date -u +%Y%m%dT%H%M%SZ)}"
BASE_IMAGE="${BASE_IMAGE:-ros:humble-ros-base-jammy}"
IMAGE="${IMAGE:-robot-systems-perf:local}"
COMMAND="${3:-runner}"
[[ "$COMMAND" == runner || "$COMMAND" == suite ]] || { echo "Third argument must be runner or suite" >&2; exit 1; }
NETWORK="${DOCKER_NETWORK:-bridge}"
IPC="${DOCKER_IPC:-private}"
HOST_BSP=""
HOST_BOARD=""
if [[ -r /etc/nv_tegra_release ]]; then HOST_BSP="$(cat /etc/nv_tegra_release)"; fi
if [[ -r /proc/device-tree/model ]]; then HOST_BOARD="$(tr -d '\000' </proc/device-tree/model)"; fi
if [[ "$CONFIG" != /* ]]; then CONFIG="$ROOT/$CONFIG"; fi
if [[ "$OUTPUT" != /* ]]; then OUTPUT="$ROOT/$OUTPUT"; fi
[[ -f "$CONFIG" ]] || { echo "Config file not found: $CONFIG" >&2; exit 1; }
[[ ! -e "$OUTPUT" ]] || { echo "Output must be a new directory: $OUTPUT" >&2; exit 1; }
mkdir -p "$(dirname "$OUTPUT")"
HOST_PROFILE="$(dirname "$OUTPUT")/$(basename "$OUTPUT")-host.json"
PROBE_ARGS=()
if [[ "${REQUIRE_JETSON:-0}" == 1 ]]; then PROBE_ARGS+=(--require-jetson); fi
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m perfkit.platform_probe --output "$HOST_PROFILE" ${PROBE_ARGS[@]+"${PROBE_ARGS[@]}"}
HOST_PROFILE="$(cd "$(dirname "$HOST_PROFILE")" && pwd)/$(basename "$HOST_PROFILE")"
ENGINE_OS="$(docker info --format '{{.OperatingSystem}}')"
ENGINE_ARCH="$(docker version --format '{{.Server.Arch}}')"
TARGET_PLATFORM="linux/$ENGINE_ARCH"
ENVIRONMENT_KIND="${ENVIRONMENT_KIND:-docker-linux-unclassified}"
if [[ "$ENGINE_OS" == *"Docker Desktop"* ]]; then ENVIRONMENT_KIND=docker-desktop-linux-vm; fi
REVISION="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf uncommitted)"
if ! BASE_ARCH="$(docker image inspect "$BASE_IMAGE" --format '{{.Architecture}}' 2>/dev/null)" || [[ "$BASE_ARCH" != "$ENGINE_ARCH" ]]; then
  docker pull --platform "$TARGET_PLATFORM" "$BASE_IMAGE"
fi
BASE_ID="$(docker image inspect "$BASE_IMAGE" --format '{{.Id}}')"
docker build --platform "$TARGET_PLATFORM" --build-arg "BASE_IMAGE=$BASE_IMAGE" --build-arg "SOURCE_REVISION=$REVISION" \
  --build-arg "BASE_IMAGE_ID=$BASE_ID" -t "$IMAGE" "$ROOT"
IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
IMAGE_ARCH="$(docker image inspect "$IMAGE" --format '{{.Architecture}}')"
[[ "$IMAGE_ARCH" == "$ENGINE_ARCH" ]] || { echo "Refusing an emulated benchmark image" >&2; exit 1; }
# Mount the parent so the runner can create a fresh, exclusive run directory.
OUTPUT_PARENT="$(cd "$(dirname "$OUTPUT")" && pwd)"
OUTPUT_NAME="$(basename "$OUTPUT")"
RESOURCE_ARGS=()
if [[ -n "${DOCKER_CPUSET:-}" ]]; then RESOURCE_ARGS+=(--cpuset-cpus "$DOCKER_CPUSET"); fi
if [[ -n "${DOCKER_CPUS:-}" ]]; then RESOURCE_ARGS+=(--cpus "$DOCKER_CPUS"); fi
docker run --rm --init --platform "$TARGET_PLATFORM" --user "$(id -u):$(id -g)" --network "$NETWORK" --ipc "$IPC" \
  ${RESOURCE_ARGS[@]+"${RESOURCE_ARGS[@]}"} \
  -e "EP_ENVIRONMENT_KIND=$ENVIRONMENT_KIND" -e "EP_IMAGE_ID=$IMAGE_ID" \
  -e "EP_DOCKER_ENGINE_OS=$ENGINE_OS" -e "EP_DOCKER_NETWORK=$NETWORK" -e "EP_DOCKER_IPC=$IPC" \
  -e "EP_HOST_KERNEL=$(uname -r)" -e "EP_HOST_ARCH=$(uname -m)" \
  -e "EP_HOST_BSP=$HOST_BSP" -e "EP_HOST_BOARD=$HOST_BOARD" \
  -e "EP_HOST_PROFILE=/host-profile.json" \
  -e "RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}" \
  --mount "type=bind,src=$OUTPUT_PARENT,dst=/results" \
  --mount "type=bind,src=$CONFIG,dst=/experiment.json,readonly" \
  --mount "type=bind,src=$HOST_PROFILE,dst=/host-profile.json,readonly" \
  "$IMAGE" python3 -m "perfkit.$COMMAND" --config /experiment.json --output "/results/$OUTPUT_NAME"
