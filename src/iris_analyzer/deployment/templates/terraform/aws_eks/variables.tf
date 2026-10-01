variable "region" {
  type = string
}

variable "cluster_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.cluster_name))
    error_message = "cluster_name must be a controlled DNS name of 3 to 40 characters."
  }
}

variable "kubernetes_version" {
  type = string
  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "An explicit supported EKS Kubernetes version is required."
  }
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(distinct(var.private_subnet_ids)) >= 2
    error_message = "At least two existing private subnets in distinct availability zones are required."
  }
}

variable "administrator_role_arn" {
  type = string
}

variable "instance_type" {
  type = string
}

variable "architecture" {
  type = string
  validation {
    condition     = contains(["amd64", "arm64"], var.architecture)
    error_message = "architecture must be amd64 or arm64."
  }
}

variable "nodes" {
  type = object({ min = number, desired = number, max = number })
  validation {
    condition     = var.nodes.min >= 1 && var.nodes.min <= var.nodes.desired && var.nodes.desired <= var.nodes.max && var.nodes.max <= 100
    error_message = "Node counts must satisfy 1 <= min <= desired <= max <= 100."
  }
}

variable "node_disk_gib" {
  type = number
  validation {
    condition     = var.node_disk_gib >= 20 && var.node_disk_gib <= 1024
    error_message = "Encrypted gp3 node disk must be 20 to 1024 GiB."
  }
}
