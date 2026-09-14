#!/usr/bin/env bash
set -Eeuo pipefail

readonly repository_url="https://github.com/Bambale0/crm_for_max.git"
readonly checkout_dir="/srv/max_admin"
readonly project_name="max_admin"
readonly health_url="https://dev.xn--e1aikcel5c5a.online/health/ready"

requested_command="${SSH_ORIGINAL_COMMAND:-$*}"
if [[ ! "$requested_command" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  echo "Rejected deploy command" >&2
  exit 64
fi
requested_sha="${BASH_REMATCH[1]}"

exec 9>/run/lock/max-admin-deploy.lock
if ! flock -n 9; then
  echo "Another max_admin deployment is active" >&2
  exit 75
fi

if [[ ! -d "$checkout_dir/.git" ]]; then
  echo "Deployment checkout is missing" >&2
  exit 78
fi
if [[ ! -s "$checkout_dir/.env" ]]; then
  echo "Deployment environment is missing" >&2
  exit 78
fi

cd "$checkout_dir"
git fetch --quiet --prune origin main
remote_sha="$(git rev-parse origin/main)"
if [[ "$requested_sha" != "$remote_sha" ]]; then
  echo "Requested revision is not the current origin/main" >&2
  exit 65
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Deployment checkout has tracked changes" >&2
  exit 78
fi

previous_sha="$(git rev-parse HEAD)"

deploy_release() (
  set -Eeuo pipefail
  git checkout --quiet --detach "$requested_sha"
  docker compose -p "$project_name" config --quiet
  docker compose -p "$project_name" build api bot-worker
  docker compose -p "$project_name" run --rm api alembic upgrade head
  docker compose -p "$project_name" --profile bot up \
    --detach --remove-orphans --wait api bot-worker
  curl --noproxy '*' --fail --silent --show-error "$health_url"
)

rollback_release() (
  set -Eeuo pipefail
  git checkout --quiet --detach "$previous_sha"
  docker compose -p "$project_name" config --quiet
  docker compose -p "$project_name" build api bot-worker
  docker compose -p "$project_name" --profile bot up \
    --detach --remove-orphans --wait api bot-worker
  curl --noproxy '*' --fail --silent --show-error "$health_url"
)

echo "Deploying $requested_sha"
if deploy_release; then
  install -d -m 700 /var/lib/max-admin
  printf '%s\n' "$requested_sha" > /var/lib/max-admin/deployed-sha
  echo
  echo "Deployment completed"
  docker compose -p "$project_name" ps
  exit 0
fi

echo "Deployment failed; restoring $previous_sha" >&2
if rollback_release; then
  echo "Previous application revision restored" >&2
else
  echo "Automatic application rollback failed" >&2
fi
exit 1
