terraform {
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.6.0"
    }
  }
}

provider "kind" {}

resource "kind_cluster" "jetstream" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"

      kubeadm_config_patches = [
        "kind: InitConfiguration\nnodeRegistration:\n  kubeletExtraArgs:\n    node-labels: \"ingress-ready=true\"\n"
      ]

      extra_port_mappings {
        container_port = 80
        host_port      = var.http_host_port
      }
    }

    dynamic "node" {
      for_each = range(var.agent_count)
      content {
        role = "worker"
      }
    }
  }
}
