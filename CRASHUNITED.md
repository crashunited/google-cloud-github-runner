# crashunited fork

Deploys the GitHub Actions runners for this org. `upstream` is
[Cyclenerd/google-cloud-github-runner](https://github.com/Cyclenerd/google-cloud-github-runner);
this branch carries local patches on top of it.

## Patches on this branch

| Area | Why |
|---|---|
| `gcp/startup/install.sh` | Sets `USERGROUPS_ENAB no` so the runner umask is 022. Ubuntu's pam_umask otherwise widens it to 002 for a user whose primary group matches its name, and the resulting group-writable directories fail permission assertions that pass on GitHub-hosted runners. |
| `gcp/startup/install.sh` | Provides `/opt/hostedtoolcache`, `ImageOS`, `RUNNER_TOOL_CACHE` and `AGENT_TOOLSDIRECTORY`, and bakes the Node majors the workflows request. |
| `gcp/startup/install.sh` | Pulls Docker Hub images through `mirror.gcr.io`, for the daemon and for buildx builders. Every runner is behind one Cloud NAT address, so Docker Hub's per-address limit on anonymous pulls would otherwise apply to the whole fleet. |
| `gcp/build-image.template.sh` | Deletes a family's previous images after a bake. Templates reference the family, so only the newest image is used. |
| `gcp/net-cloudnat.tf` | Dynamic port allocation and `ERRORS_ONLY` logging. The 64-port default is not enough for jobs opening many parallel connections. |
| `tools/reconcile.py`, `gcp/reconcile.tf` | Scheduled Cloud Run job. The manager creates one VM per `workflow_job` queued webhook and GitHub never redelivers it, so a VM that fails to register strands its job. Also reaps VMs whose runner never appeared and runners left idle. Scans only the runner group's repositories, only runs created in the last 7 days, eight repositories at a time, and logs how long each phase took. Stops creating VMs at a compute quota error and leaves the rest of the queue for a later tick. The per-family CPU quota (`E2_CPUS` for the default `e2-standard-4`) is the fleet size cap. |
| `gcp/cloud-run.tf` | Requires a real `SETUP_PASSWORD`. The default falls back to the project id, and completing `/setup/` rewrites the stored GitHub App credentials. |
| `.dockerignore`, `.gcloudignore` | Allow `tools/` into the container so the reconcile job ships. |

## Syncing with upstream

```sh
git fetch upstream
git rebase upstream/master
```

Resolve conflicts in the files above, then force-push this branch. Re-bake the
images afterwards, because `install.sh` reaches VMs through a GCS object whose
hash is not part of the build trigger:

```sh
cd gcp
tofu apply \
  -replace='null_resource.build-github-runners-images["ubuntu-2404-lts-amd64"]' \
  -replace='null_resource.build-github-runners-images["ubuntu-2404-lts-arm64"]'
```

Changes under `app/` or `tools/` need the container rebuilt instead:

```sh
tofu apply -replace='null_resource.build-github-runners-manager-container'
```

## State and configuration

State lives in the GCS backend configured in `gcp/providers.tf`.
`gcp/terraform.tfvars` is gitignored and backed up under `config/` in that same
bucket.
