terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.14.1"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      ManagedBy       = "iris-fixed-template"
      TemplateVersion = "iris.execution.v1"
    }
  }
}
