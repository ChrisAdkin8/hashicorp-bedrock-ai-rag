#!/usr/bin/env bash
# clone_repos.sh — Shallow-clone HashiCorp GitHub repos in parallel.
# Outputs cloned repos to /codebuild/output/repos/.
set -euo pipefail

REPOS_DIR="/codebuild/output/repos"
mkdir -p "${REPOS_DIR}"

declare -A CORE_REPOS=(
  ["terraform"]="https://github.com/hashicorp/terraform.git"
  ["vault"]="https://github.com/hashicorp/vault.git"
  ["consul"]="https://github.com/hashicorp/consul.git"
  ["nomad"]="https://github.com/hashicorp/nomad.git"
  ["packer"]="https://github.com/hashicorp/packer.git"
  ["boundary"]="https://github.com/hashicorp/boundary.git"
  ["waypoint"]="https://github.com/hashicorp/waypoint.git"
  ["terraform-docs-agents"]="https://github.com/hashicorp/terraform-docs-agents.git"
  ["terraform-website"]="https://github.com/hashicorp/terraform-website.git"
)

declare -A PROVIDER_REPOS=(
  ["terraform-provider-aws"]="https://github.com/hashicorp/terraform-provider-aws.git"
  ["terraform-provider-azurerm"]="https://github.com/hashicorp/terraform-provider-azurerm.git"
  ["terraform-provider-google"]="https://github.com/hashicorp/terraform-provider-google.git"
  ["terraform-provider-kubernetes"]="https://github.com/hashicorp/terraform-provider-kubernetes.git"
  ["terraform-provider-helm"]="https://github.com/hashicorp/terraform-provider-helm.git"
  ["terraform-provider-docker"]="https://github.com/kreuzwerker/terraform-provider-docker.git"
  ["terraform-provider-vault"]="https://github.com/hashicorp/terraform-provider-vault.git"
  ["terraform-provider-consul"]="https://github.com/hashicorp/terraform-provider-consul.git"
  ["terraform-provider-nomad"]="https://github.com/hashicorp/terraform-provider-nomad.git"
  ["terraform-provider-random"]="https://github.com/hashicorp/terraform-provider-random.git"
  ["terraform-provider-null"]="https://github.com/hashicorp/terraform-provider-null.git"
  ["terraform-provider-local"]="https://github.com/hashicorp/terraform-provider-local.git"
  ["terraform-provider-tls"]="https://github.com/hashicorp/terraform-provider-tls.git"
  ["terraform-provider-http"]="https://github.com/hashicorp/terraform-provider-http.git"
)

declare -A SENTINEL_REPOS=(
  ["sentinel-policies"]="https://github.com/hashicorp/sentinel-policies.git"
  ["terraform-sentinel-policies"]="https://github.com/hashicorp/terraform-sentinel-policies.git"
  ["vault-sentinel-policies"]="https://github.com/hashicorp/vault-sentinel-policies.git"
  ["consul-sentinel-policies"]="https://github.com/hashicorp/consul-sentinel-policies.git"
)

clone_repo() {
  local name="$1"
  local url="$2"
  local dest="${REPOS_DIR}/${name}"

  if [[ -d "${dest}/.git" ]]; then
    echo "Skipping ${name} — already cloned"
    return 0
  fi

  echo "Cloning ${name}..."
  git clone --depth 1 --single-branch "${url}" "${dest}" 2>&1 || {
    echo "WARN: Failed to clone ${name} from ${url} — skipping"
  }
}

export -f clone_repo
export REPOS_DIR

# Clone all repo groups in parallel
pids=()

for name in "${!CORE_REPOS[@]}"; do
  clone_repo "${name}" "${CORE_REPOS[$name]}" &
  pids+=($!)
done

for name in "${!PROVIDER_REPOS[@]}"; do
  clone_repo "${name}" "${PROVIDER_REPOS[$name]}" &
  pids+=($!)
done

for name in "${!SENTINEL_REPOS[@]}"; do
  clone_repo "${name}" "${SENTINEL_REPOS[$name]}" &
  pids+=($!)
done

# Wait for all parallel clones to complete
failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    failed=$((failed + 1))
  fi
done

if [[ "${failed}" -gt 0 ]]; then
  echo "WARN: ${failed} clone(s) failed — check output above. Continuing with available repos."
fi

echo "Clone phase complete. Repos in ${REPOS_DIR}:"
ls "${REPOS_DIR}"
