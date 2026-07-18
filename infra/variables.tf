variable "cluster_name" {
  description = "Name of the kind cluster."
  type        = string
  default     = "jetstream-cluster"
}

variable "agent_count" {
  description = "Number of worker nodes."
  type        = number
  default     = 3
}

variable "http_host_port" {
  description = "Host port mapped to the control-plane node's ingress port (container port 80)."
  type        = number
  default     = 8080
}
