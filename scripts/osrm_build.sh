#!/usr/bin/env bash
# Build self-hosted OSRM routing data for Tajikistan (car + foot profiles).
#
# Runs OFF the production server (preprocessing peaks well above the server's
# RAM budget) — on a workstation with Docker. The server only runs osrm-routed
# on the prepared files (docker-compose.yml profile "osrm"; DEPLOY.md).
#
#   scripts/osrm_build.sh            # download extract (if missing) + build both
#   scripts/osrm_build.sh car        # one profile only
#
# Output: osrm-data/{car,foot}/tajikistan-latest.osrm* (gitignored). The image
# tag MUST match the one osrm-routed uses in docker-compose.yml — the data
# format is versioned. linux/amd64 because the server is x86_64.
set -euo pipefail

IMAGE="ghcr.io/project-osrm/osrm-backend:v6.0.0"
PLATFORM="linux/amd64"
EXTRACT_URL="https://download.geofabrik.de/asia/tajikistan-latest.osm.pbf"
ROOT="$(cd "$(dirname "$0")/.." && pwd)/osrm-data"
PBF="tajikistan-latest.osm.pbf"
if [ $# -eq 0 ]; then set -- car foot; fi
PROFILES=("$@")

mkdir -p "$ROOT"
if [ ! -s "$ROOT/$PBF" ]; then
    echo "Downloading $EXTRACT_URL"
    curl -fL --retry 5 -C - -o "$ROOT/$PBF" "$EXTRACT_URL"
fi

for profile in "${PROFILES[@]}"; do
    case "$profile" in car|foot) ;; *) echo "unknown profile: $profile" >&2; exit 2 ;; esac
    dir="$ROOT/$profile"
    rm -rf "$dir"
    mkdir -p "$dir"
    cp "$ROOT/$PBF" "$dir/$PBF"
    run() { docker run --rm --platform "$PLATFORM" -v "$dir:/data" "$IMAGE" "$@"; }
    echo "== $profile: extract"
    run osrm-extract -p "/opt/$profile.lua" "/data/$PBF"
    echo "== $profile: partition"
    run osrm-partition "/data/${PBF%.osm.pbf}.osrm"
    echo "== $profile: customize"
    run osrm-customize "/data/${PBF%.osm.pbf}.osrm"
    rm -f "$dir/$PBF"
    du -sh "$dir"
done

echo "Done. Copy osrm-data/ to the server (DEPLOY.md, OSRM section)."
