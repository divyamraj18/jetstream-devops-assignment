output "cluster_name" {
  description = "Name of the created kind cluster."
  value       = kind_cluster.jetstream.name
}

output "kube_context" {
  description = "kubectl context name for the created cluster."
  value       = "kind-${kind_cluster.jetstream.name}"
}

output "endpoint" {
  description = "Kubernetes APIServer endpoint."
  value       = kind_cluster.jetstream.endpoint
  sensitive   = true
}

output "http_endpoint" {
  description = "Local endpoint mapped to the control-plane's ingress port (port 80)."
  value       = "http://localhost:${var.http_host_port}"
}
