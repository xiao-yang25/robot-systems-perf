#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-configs/smoke.json}"
OUTPUT="${2:-results/native-$(date -u +%Y%m%dT%H%M%SZ)}"
COMMAND="${3:-runner}"
[[ "$(uname -s)" == Linux ]] || { echo "Native measurements require Linux" >&2; exit 1; }
[[ "$COMMAND" == runner || "$COMMAND" == suite ]] || { echo "Third argument must be runner or suite" >&2; exit 1; }
if [[ "$CONFIG" != /* ]]; then CONFIG="$ROOT/$CONFIG"; fi
if [[ "$OUTPUT" != /* ]]; then OUTPUT="$ROOT/$OUTPUT"; fi
[[ -f "$CONFIG" && ! -e "$OUTPUT" ]] || { echo "Config required; output must be a new directory" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required for native runs"'
if [[ -z "${AMENT_PREFIX_PATH:-}" ]]; then
  ROS_SETUP="/opt/ros/${ROS_DISTRO:-humble}/setup.bash"
  [[ -f "$ROS_SETUP" ]] || { echo "Install a compatible ROS environment or use Docker" >&2; exit 1; }
  set +u
  source "$ROS_SETUP"
  set -u
fi
cd "$ROOT"
mkdir -p "$(dirname "$OUTPUT")"
HOST_PROFILE="$(dirname "$OUTPUT")/$(basename "$OUTPUT")-host.json"
PROBE_ARGS=()
if [[ "${REQUIRE_JETSON:-0}" == 1 ]]; then PROBE_ARGS+=(--require-jetson); fi
python3 -m perfkit.platform_probe --output "$HOST_PROFILE" ${PROBE_ARGS[@]+"${PROBE_ARGS[@]}"}
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
EP_HOST_PROFILE="$HOST_PROFILE" EP_ENVIRONMENT_KIND="${ENVIRONMENT_KIND:-native-linux}" \
  python3 -m "perfkit.$COMMAND" --config "$CONFIG" --output "$OUTPUT"
