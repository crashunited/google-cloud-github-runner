# Cloud NAT for GitHub Actions Runners to access the internet
# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/net-cloudnat/README.md
module "nat-github-runners" {
  source         = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v53.0.0"
  project_id     = module.project.project_id
  region         = var.region
  name           = "cloudnat-github-runners-${local.region_shortnames[var.region]}"
  router_network = module.vpc-github-runners.self_link

  # 64 ports per VM is not enough for jobs that open many parallel
  # connections (package installs, image pulls); exhaustion surfaces as
  # intermittent connection failures.
  config_port_allocation = {
    enable_dynamic_port_allocation      = true
    enable_endpoint_independent_mapping = false
    min_ports_per_vm                    = 128
    max_ports_per_vm                    = 65536
  }

  logging_filter = "ERRORS_ONLY"
}
