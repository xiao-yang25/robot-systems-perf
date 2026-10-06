#!/usr/bin/env bash
# Use the selected interpreter with the SDK environment already loaded by the operator.
set -euo pipefail
readonly perfkit_regression_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly -a perfkit_regression_args=("$@")
perfkit_regression_python=python3
while (($#)); do
  case "$1" in
    --ros-python=*) perfkit_regression_python="${1#*=}"; shift ;;
    --ros-python) perfkit_regression_python="$2"; shift 2 ;;
    *) shift ;;
  esac
done
readonly perfkit_regression_python
exec "$perfkit_regression_python" "$perfkit_regression_root/tests/device_regression.py" "${perfkit_regression_args[@]}"
