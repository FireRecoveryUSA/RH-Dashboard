#!/usr/bin/env bash
# Build, run, and monitor the RecoveryHub Dashboard stack locally in Docker.
#
# Linux/macOS port of dev-start.ps1 — a thin wrapper over `docker compose` for
# the services defined in docker-compose.yml: MongoDB, the FastAPI backend, and
# the nginx frontend. The frontend nginx container proxies /api/* to the
# backend container over a shared docker network.
#
# Configuration (Azure, Mongo, AI credentials, and the VITE_* build args)
# comes from the local .env file, which Compose reads automatically.
#
# MongoDB uses a named volume (rhdashboard_mongo-data) so saved dashboards
# persist across restarts.
#
# NOTE: MONGODB_URI in .env determines which database the backend uses. If it
# points at an Atlas cluster, the local mongo container is unused. Set
# MONGODB_URI=mongodb://mongo:27017 to use the local container instead.
#
# Usage:
#   ./dev-start.sh              # Build + run (default)
#   ./dev-start.sh --no-cache   # Full clean rebuild + restart
#   ./dev-start.sh --restart    # Just restart existing containers
#   ./dev-start.sh --stop       # Stop and remove containers
#   ./dev-start.sh --logs       # Follow all logs
#   ./dev-start.sh --logs --service backend   # Follow backend logs only
#   ./dev-start.sh --follow     # Build + run, then follow logs
#   ./dev-start.sh --logs --tail all          # Follow with full history
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# Every docker call is scoped to this repo's compose file so the script works
# regardless of the caller's working directory.
COMPOSE=(compose --project-directory "$REPO_ROOT" -f "$REPO_ROOT/docker-compose.yml")

section() { printf '\n=== %s ===\n' "$1"; }
die()     { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

invoke_compose() {
    docker "${COMPOSE[@]}" "$@"
}

check_docker_ready() {
    # Verifies the Docker engine is actually responsive before we issue any
    # docker commands, so a hung engine fails fast instead of blocking forever.
    section 'Checking Docker engine'
    local version
    if ! version="$(timeout 15 docker version --format '{{.Server.Version}}' 2>&1)"; then
        die "Docker engine is not responding correctly (or within 15s).
Returned: $version

Common causes:
  - Docker daemon is still starting up (wait ~30s and retry)
  - The Docker daemon is stopped (try: sudo systemctl start docker)
  - Wrong docker context (run: docker context ls)
  - No permission on /var/run/docker.sock (add yourself to the docker group)"
    fi
    if [[ ! "$version" =~ [0-9]+\.[0-9]+ ]]; then
        die "Docker engine returned an unexpected version string: $version"
    fi
    printf 'Docker engine ready (server %s).\n' "$version"
}

check_env_file() {
    # Compose fails with an opaque interpolation error if the VITE_* build args
    # are missing, so check for them up front with an actionable message.
    [[ -f "$ENV_FILE" ]] || die "Missing .env file at $ENV_FILE"
    local key
    for key in VITE_AZURE_CLIENT_ID VITE_AZURE_TENANT_ID; do
        grep -Eq "^[[:space:]]*$key[[:space:]]*=[[:space:]]*[^[:space:]]" "$ENV_FILE" ||
            die "Missing '$key' in $ENV_FILE (required as a frontend build arg)."
    done
}

show_logs() {
    # `docker compose logs` can return immediately with no useful context when
    # there are no running containers. Check the Compose-managed services first
    # and give the user an actionable error instead.
    local running=()
    mapfile -t running < <(docker "${COMPOSE[@]}" ps --format '{{.Service}}' 2>/dev/null) ||
        die 'Could not inspect the Compose stack before following logs.'
    if [[ ${#running[@]} -eq 0 ]]; then
        die "No RecoveryHub Dashboard containers are running. Start the stack first: ./dev-start.sh"
    fi

    local svc missing=()
    for svc in ${SERVICES[@]:-}; do
        [[ " ${running[*]} " == *" $svc "* ]] || missing+=("$svc")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Requested service(s) are not running: ${missing[*]}. Running services: ${running[*]}."
    fi

    section 'Following logs (Ctrl+C to stop; containers keep running)'
    # Not wrapped in error handling: Ctrl+C makes `logs -f` exit non-zero,
    # which is the normal way to stop following and must not be treated as a
    # failure. `|| true` keeps `set -e` from aborting the script on it.
    docker "${COMPOSE[@]}" logs -f --tail "$TAIL" ${SERVICES[@]:-} || true
}

show_endpoints() {
    printf '\n--- Dev stack running ---\n'
    printf 'Frontend:  http://localhost:3000\n'
    printf 'Backend:   http://localhost:8001/docs\n'
    printf 'MongoDB:   localhost:27017\n'
    printf '\nLogs:\n  ./dev-start.sh --logs\n'
    printf '\nStop:\n  ./dev-start.sh --stop\n'
}

# ---------------------------------------------------------------------------
# Argument parsing (mirrors the PowerShell parameter sets)
# ---------------------------------------------------------------------------
MODE=build        # build | restart | stop | logs
NOCACHE=0
FOLLOW=0
TAIL=50
SERVICES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build)      MODE=build ;;
        --no-cache)   MODE=build; NOCACHE=1 ;;
        --restart)    MODE=restart ;;
        --stop)       MODE=stop ;;
        --logs)       MODE=logs ;;
        --follow)     FOLLOW=1 ;;
        --service)    shift; IFS=',' read -ra SERVICES <<< "$1" ;;
        --service=*)  IFS=',' read -ra SERVICES <<< "${1#*=}" ;;
        --tail)       shift; TAIL="$1" ;;
        --tail=*)     TAIL="${1#*=}" ;;
        -h|--help)    sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
done

for svc in "${SERVICES[@]:-}"; do
    [[ -z "$svc" || "$svc" =~ ^(mongo|backend|frontend)$ ]] ||
        die "Invalid --service value '$svc' (expected: mongo, backend, or frontend)"
done
[[ "$TAIL" == all || "$TAIL" =~ ^[0-9]+$ ]] || die "--tail must be a number or 'all'"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Fail fast if the Docker engine isn't actually up.
check_docker_ready

if [[ "$MODE" == logs ]]; then
    show_logs
    exit 0
fi

if [[ "$MODE" == stop ]]; then
    section 'Stopping dev containers'
    # No -v: the mongo-data volume is preserved so dashboards survive.
    invoke_compose down --remove-orphans
    echo 'Done.'
    exit 0
fi

check_env_file

if [[ "$MODE" != restart ]]; then
    section 'Building images'
    if [[ "$NOCACHE" -eq 1 ]]; then
        invoke_compose build --no-cache
    else
        invoke_compose build
    fi
fi

section 'Starting dev containers'
# Recreates containers whose config or image changed. --wait blocks until the
# healthchecks that gate depends_on report healthy, so the endpoints printed
# below are actually serving by the time the script returns.
invoke_compose up -d --remove-orphans --wait

show_endpoints

if [[ "$FOLLOW" -eq 1 ]]; then show_logs; fi
