#!/usr/bin/env bash

# Copyright 2025-2026 Nils Knieling. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Install Docker and GitHub Actions Runner for Linux with x64 or ARM64 CPU architecture
# https://github.com/actions/runner
# https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#linux
# https://docs.docker.com/engine/install/ubuntu/

# Exit on error, undefined variables, and pipe failures
set -euo pipefail

# Set default GitHub Actions Runner installation directory
readonly MY_RUNNER_DIR="/actions-runner"

# Prevent interactive prompts during package installation
export DEBIAN_FRONTEND=noninteractive

# Function to exit the script with a failure message
exit_with_failure() {
	echo >&2 "FAILURE: $1"
	exit 1
}

# Detect CPU architecture early
case $(uname -m) in
	aarch64|arm64)
		readonly MY_ARCH="arm64"
		;;
	amd64|x86_64)
		readonly MY_ARCH="x64"
		;;
	*)
		exit_with_failure "Cannot determine CPU architecture!"
		;;
esac

# Install dependencies
echo "Installing system dependencies..."
sudo apt-get update -yq
sudo apt-get install -y \
	apt-transport-https \
	apt-utils \
	build-essential \
	ca-certificates \
	curl \
	dnsutils \
	git \
	gpg \
	jq \
	lsb-release \
	nodejs \
	npm \
	openssh-client \
	python3-crcmod \
	python3-openssl \
	python3-pip \
	python3-venv \
	software-properties-common \
	tar \
	unzip \
	zip

# Verify required commands are available
readonly REQUIRED_COMMANDS=(curl gzip jq sed tar)
for cmd in "${REQUIRED_COMMANDS[@]}"; do
	if ! command -v "$cmd" >/dev/null 2>&1; then
		exit_with_failure "Required command '$cmd' not found"
	fi
done

# Add Docker repository and install
echo "Installing Docker..."
sudo curl -fsSL "https://download.docker.com/linux/ubuntu/gpg" | sudo gpg --dearmor -o "/usr/share/keyrings/download.docker.com"
echo "deb [signed-by=/usr/share/keyrings/download.docker.com] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee "/etc/apt/sources.list.d/docker.list" >/dev/null
sudo apt-get update -yq
sudo apt-get install -y \
	docker-ce \
	docker-ce-cli \
	containerd.io \
	docker-buildx-plugin \
	docker-compose-plugin

# Enable and start Docker service
sudo systemctl enable docker.service
sudo systemctl start docker.service

# Create runner user and add to docker und sudoers group
echo "Creating runner user..."
if ! id -u runner >/dev/null 2>&1; then
	sudo useradd -m runner
fi
sudo usermod -aG docker,google-sudoers runner

# Use umask 022, matching GitHub-hosted runners. /etc/login.defs sets
# UMASK 022, but USERGROUPS_ENAB yes lets pam_umask widen it to 002 for
# users whose primary group name matches their username, which makes new
# directories group-writable.
sudo sed -i 's/^USERGROUPS_ENAB.*/USERGROUPS_ENAB no/' /etc/login.defs
grep -q '^USERGROUPS_ENAB no$' /etc/login.defs || exit_with_failure "could not set USERGROUPS_ENAB"

# Install GitHub Actions Runner
echo "Installing GitHub Actions Runner..."
MY_RUNNER_VERSION=$(curl -fsSL "https://api.github.com/repos/actions/runner/releases/latest" | jq -r '.tag_name' | sed 's/^v//')
if [[ -z "$MY_RUNNER_VERSION" || "$MY_RUNNER_VERSION" == "null" ]]; then
	exit_with_failure "Could not retrieve the latest GitHub Actions Runner version"
fi
echo "Installing GitHub Actions Runner version: v${MY_RUNNER_VERSION}"

# Download and extract runner
sudo mkdir -p "$MY_RUNNER_DIR"
cd "$MY_RUNNER_DIR"
sudo curl -fsSL -O "https://github.com/actions/runner/releases/download/v${MY_RUNNER_VERSION}/actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"
sudo tar xzf "actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"

# Run the installation script
sudo ./bin/installdependencies.sh
sudo chown -R runner:runner "$MY_RUNNER_DIR"
echo "GitHub Actions Runner installed successfully"

# Tool cache and image identity, which GitHub-hosted runners provide.
# actions/setup-node, setup-java, setup-python and ruby/setup-ruby read
# ImageOS to pick a prebuilt release rather than building from source, and
# expect RUNNER_TOOL_CACHE to name a writable directory. Runners are
# ephemeral, so the cache is per-VM and only pays off for versions baked
# into the image.
sudo mkdir -p /opt/hostedtoolcache
sudo chown runner:runner /opt/hostedtoolcache
sudo chmod 0755 /opt/hostedtoolcache
sudo -u runner tee "$MY_RUNNER_DIR/.env" >/dev/null <<'RUNNER_ENV'
ImageOS=ubuntu24
RUNNER_TOOL_CACHE=/opt/hostedtoolcache
AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache
RUNNER_ENV

# Bake the Node versions the workflows ask for into the tool cache so
# actions/setup-node resolves them locally instead of downloading on every
# job. Layout matches what setup-node expects: <tool>/<version>/<arch>
# alongside a .complete marker.
case "$MY_ARCH" in
	x64) MY_NODE_ARCH="x64" ;;
	arm64) MY_NODE_ARCH="arm64" ;;
	*) exit_with_failure "unsupported architecture for the Node tool cache: $MY_ARCH" ;;
esac
for MY_NODE_MAJOR in 22 24; do
	MY_NODE_VERSION=$(curl -fsSL "https://nodejs.org/dist/index.json" \
		| jq -r --arg major "v${MY_NODE_MAJOR}." \
			'[.[] | select(.version | startswith($major))] | first | .version')
	if [[ -z "$MY_NODE_VERSION" || "$MY_NODE_VERSION" == "null" ]]; then
		exit_with_failure "could not resolve the latest Node ${MY_NODE_MAJOR} release"
	fi
	MY_NODE_DIR="/opt/hostedtoolcache/node/${MY_NODE_VERSION#v}/${MY_NODE_ARCH}"
	echo "Caching Node ${MY_NODE_VERSION} for ${MY_NODE_ARCH}..."
	sudo -u runner mkdir -p "$MY_NODE_DIR"
	curl -fsSL "https://nodejs.org/dist/${MY_NODE_VERSION}/node-${MY_NODE_VERSION}-linux-${MY_NODE_ARCH}.tar.xz" \
		| sudo -u runner tar -xJ --strip-components=1 -C "$MY_NODE_DIR"
	sudo -u runner touch "/opt/hostedtoolcache/node/${MY_NODE_VERSION#v}/${MY_NODE_ARCH}.complete"
done

# Cleanup: Clear package cache and temporary files
echo "Cleaning up..."
sudo apt-get clean
sudo rm -rf /tmp/* /root/.cache

# Cleanup: Rotate and vacuum journal logs
sudo journalctl --rotate
sudo journalctl --vacuum-time=1s

# Cleanup: Remove compressed and rotated log files, then truncate remaining logs
sudo find /var/log -type f \( -name "*.gz" -o -regex ".*\.[0-9]$" \) -delete
sudo find /var/log -type f -exec truncate -s 0 {} +

echo "Setup completed successfully"

# Shutdown VM
sudo shutdown -h now
