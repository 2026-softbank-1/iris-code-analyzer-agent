# Fixed execution templates — `iris.execution.v1`

The compiler accepts the deployment plan contract and validates it again before
producing execution inputs. Draft recommendations never produce runnable files.
Changing a target stack requires another explicit template adapter; unknown
stacks remain machine-readable unsupported results.

## Terraform

`terraform/aws_eks` uses the pinned `hashicorp/aws` 6.14.1 provider to create a
private EKS control plane, explicit administrator access, managed worker nodes,
and encrypted gp3 node disks in an **existing VPC and private subnets**. Terraform
data and checks verify VPC membership and at least two availability zones.
Cluster version, IAM administrator role, subnet IDs, node sizing, and selected
architecture are typed inputs; none are replaced with guessed resource IDs.

The executor must already reach the private API and supply AWS authentication.
The network must provide private subnet egress or the necessary VPC endpoints.
Application databases, NAT gateways, Ingress controllers, CSI drivers, DNS,
certificates, state backends, and cloud credentials are external prerequisites,
not resources secretly provisioned by this module. The fixed module does not
contain application shell commands, provisioners, or AI-produced HCL.

## Helm / Kubernetes

`helm/iris-app` renders resource objects created by the compiler, with chart
schema checks, resource-kind and namespace checks, and **no `tpl` evaluation**.
The exact same objects are returned as native Kubernetes manifests, so Helm and
native output agree. Generated kinds are Deployment, ClusterIP Service, TLS
Ingress, PersistentVolumeClaim, and PodDisruptionBudget. Namespace creation is
an explicit executor policy (`createNamespace=true`), rather than a Helm-owned
Namespace resource that would conflict with existing or precreated namespaces.

The compiler requires immutable container image digests and confirmed image
architecture. It uses the image entrypoint; source-derived commands are never
inserted into shell scripts. Resource quantities, replicas, probes, rollout
parameters, reference-only Secrets, storage bindings, and ingress bindings must
be explicitly validated in the plan. Secret values are never created or stored.
An explicit `claimName` always references an existing claim; no PVC is emitted
and no resize/adoption occurs. A null `claimName` plus explicit `sizeGiB` and
`storageClass` requests a new PVC with a deterministic workload/volume name.
Replicated workloads get a PDB and a soft preference for placement in different
zones. This does not guarantee zone distribution or a high-availability SLO;
actual labels, worker placement, dependency availability and failure tests remain
part of runtime verification.

Rendering configuration is separate from deployment authorization and does not
prove a workload builds, starts, meets an SLO, or survives a rolling upgrade.

Primary references used for this template:

- [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Kubernetes container resources](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [AWS provider EKS node groups](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/eks_node_group)
