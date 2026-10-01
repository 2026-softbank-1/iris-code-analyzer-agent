# Fixed template: provision a private EKS cluster in an existing network.
# No application build, shell provisioner, credentials, or arbitrary user HCL.
data "aws_vpc" "selected" {
  id = var.vpc_id
}

data "aws_subnet" "selected" {
  for_each = toset(var.private_subnet_ids)
  id       = each.value
}

resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = false
  }
  enabled_cluster_log_types = ["api", "audit", "authenticator"]
  depends_on                = [aws_iam_role_policy_attachment.cluster]
  lifecycle {
    precondition {
      condition = alltrue([
        for subnet in data.aws_subnet.selected : subnet.vpc_id == data.aws_vpc.selected.id
      ]) && length(toset([for subnet in data.aws_subnet.selected : subnet.availability_zone])) >= 2
      error_message = "Subnets must belong to the selected VPC and span at least two availability zones."
    }
  }
}

resource "aws_eks_access_entry" "administrator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.administrator_role_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "administrator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.administrator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

resource "aws_iam_role" "nodes" {
  name = "${var.cluster_name}-nodes"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
  ])
  role       = aws_iam_role.nodes.name
  policy_arn = each.value
}

resource "aws_launch_template" "nodes" {
  name_prefix = "${var.cluster_name}-nodes-"
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.node_disk_gib
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.cluster_name}-workers"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = [var.instance_type]
  ami_type        = var.architecture == "arm64" ? "AL2023_ARM_64_STANDARD" : "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  launch_template {
    id      = aws_launch_template.nodes.id
    version = aws_launch_template.nodes.latest_version
  }
  scaling_config {
    min_size     = var.nodes.min
    desired_size = var.nodes.desired
    max_size     = var.nodes.max
  }
  update_config {
    max_unavailable = 1
  }
  depends_on = [aws_iam_role_policy_attachment.nodes]
}
