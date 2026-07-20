#!/bin/sh
set -eu

image="earshot:smoke"
container="earshot-smoke-$$"
volume="earshot-smoke-data-$$"
port="${EARSHOT_SMOKE_PORT:-14319}"
token="container-smoke-token"

cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build --tag "$image" .
docker volume create "$volume" >/dev/null

start_container() {
  docker run --detach \
    --name "$container" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:size=32m,mode=1777 \
    --mount "type=volume,source=$volume,target=/data" \
    --env "EARSHOT_TOKEN=$token" \
    --env "EARSHOT_HOST=0.0.0.0" \
    --env "EARSHOT_BEHIND_TLS_PROXY=true" \
    --publish "127.0.0.1:$port:4319" \
    "$image" >/dev/null
}

wait_until_ready() {
  attempt=0
  until curl --fail --silent --show-error "http://127.0.0.1:$port/readyz" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      docker logs "$container"
      exit 1
    fi
    sleep 1
  done
}

start_container
wait_until_ready

test "$(docker exec "$container" id -u)" != "0"
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container")" = "true"

unauthorized="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "http://127.0.0.1:$port/v1/incidents")"
authorized="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $token" \
  "http://127.0.0.1:$port/v1/incidents")"
test "$unauthorized" = "401"
test "$authorized" = "200"

created="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --request POST \
  --header "Authorization: Bearer $token" \
  --header "Content-Type: application/json" \
  --data-binary @fixtures/valid/minimal.json \
  "http://127.0.0.1:$port/v1/incidents")"
persisted="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $token" \
  "http://127.0.0.1:$port/v1/incidents/fixture-minimal")"
test "$created" = "201"
test "$persisted" = "200"

docker rm --force "$container" >/dev/null
start_container
wait_until_ready

after_replacement="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --header "Authorization: Bearer $token" \
  "http://127.0.0.1:$port/v1/incidents/fixture-minimal")"
test "$after_replacement" = "200"
