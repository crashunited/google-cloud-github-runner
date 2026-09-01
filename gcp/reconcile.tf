variable "github_org" {
  type        = string
  description = "GitHub organization the runners are registered to"
}

variable "reconcile_schedule" {
  type        = string
  description = "Cron schedule for the reconcile job"
  default     = "*/5 * * * *"
}

# Recreate runners for jobs whose workflow_job "queued" webhook produced a VM
# that never registered, and delete VMs whose runner never appeared. GitHub
# does not redeliver that webhook, so without this a lost VM strands its job.
module "cloud-run-github-runners-reconcile" {
  source     = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-run-v2?ref=v53.0.0"
  project_id = module.project.project_id
  name       = "github-runners-reconcile-${local.region_shortnames[var.region]}"
  type       = "JOB"
  region     = var.region
  containers = {
    reconcile = {
      image   = data.google_artifact_registry_docker_image.container-image-github-runners-manager.self_link
      command = ["python", "tools/reconcile.py"]
      env = {
        GOOGLE_CLOUD_PROJECT = var.project_id
        GOOGLE_CLOUD_ZONE    = "${var.region}-${var.zone}"
        GITHUB_RUNNER_GROUP  = var.github_runner_group
        GITHUB_ORG           = var.github_org
      }
      env_from_key = {
        GITHUB_APP_ID = {
          secret  = module.secret-manager.ids["github-app-id"]
          version = "latest"
        }
        GITHUB_INSTALLATION_ID = {
          secret  = module.secret-manager.ids["github-installation-id"]
          version = "latest"
        }
        GITHUB_PRIVATE_KEY = {
          secret  = module.secret-manager.ids["github-private-key"]
          version = "latest"
        }
      }
    }
  }
  service_account_config = {
    create = false
    email  = module.service-account-cloud-run-github-runners-manager.email
  }
  deletion_protection = false
  depends_on = [
    google_secret_manager_secret_version.secret-version-default,
    time_sleep.wait_for_service_account_cloud_run
  ]
}

resource "google_cloud_scheduler_job" "reconcile" {
  project     = module.project.project_id
  region      = var.region
  name        = "github-runners-reconcile"
  description = "Recreate runners for stranded GitHub Actions jobs"
  schedule    = var.reconcile_schedule
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${module.cloud-run-github-runners-reconcile.id}:run"
    oauth_token {
      service_account_email = module.service-account-cloud-run-github-runners-manager.email
    }
  }
}

resource "google_project_iam_member" "reconcile-invoker" {
  project = module.project.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${module.service-account-cloud-run-github-runners-manager.email}"
}

variable "setup_password" {
  type        = string
  sensitive   = true
  description = "Basic auth password for the /setup/ endpoint"
}
