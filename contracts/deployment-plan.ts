/** Versioned transport contract. JSON schemas and Python semantic validation
 * remain authoritative. No field here grants permission to deploy infrastructure.
 */
export type Architecture = "x86_64" | "arm64";
export type ImagePlatform = "amd64" | "arm64";
export type StackAdapter = "aws_eks" | "existing_kubernetes";
export type PlanningStatus = "draft" | "needs_input" | "ready" | "unsupported";
export type ProvenanceBasis = "source" | "user" | "measurement" | "policy";
export interface Provenance {
  basis: ProvenanceBasis[];
  evidenceIds: string[];
  measurementIds: string[];
  assumptionIds: string[];
  reason: string;
}
export interface Recommendation<T> {
  value: T;
  provenance: Provenance;
}
/** Positive integral millicores and MiB; memory is not bytes or megabytes. */
export interface ResourceQuantity {
  cpuMillicores: number;
  memoryMiB: number;
}
export interface ResourceAllocation {
  requests: ResourceQuantity;
  limits: ResourceQuantity;
}
export interface Target {
  stack: string | null;
  cloud: string | null;
  region: string | null;
  architecture: Architecture | null;
  environment: "test" | "production";
}
export interface Measurement {
  id: string;
  serviceId: string;
  sourceSnapshotId: string;
  kind: "build" | "test" | "load" | "runtime";
  verified: boolean;
  measuredAt: string;
  metrics: {
    peakMemoryMiB: number | null;
    cpuMillicores: number | null;
    achievedRps: number | null;
    p95LatencyMs: number | null;
    errorRate: number | null;
  };
  conditions: {
    durationSeconds: number | null;
    concurrency: number | null;
    command: string | null;
    capacityValidated?: boolean;
    architecture?: Architecture | null;
    /** Docker immutable config image ID, with sha256: prefix. */
    imageDigest?: string | null;
  };
}
export interface PricingCatalog {
  currency: "USD";
  sourceUrl: string;
  verifiedAt: string;
  verified: boolean;
  items: Array<{
    key: string;
    cloud: string;
    region: string;
    unit: "hour" | "gb_month" | "month";
    unitPriceUsd: number;
    instanceType: string | null;
    architecture: Architecture | null;
    cpuMillicores: number | null;
    memoryMiB: number | null;
    sourceUrl?: string;
  }>;
}
export interface NetworkConfiguration {
  vpcMode: "create" | "existing";
  vpcId: string | null;
  subnetIds: string[];
  availabilityZones: string[];
  natGatewayCount: number;
  publicIngress: boolean;
}
export interface DatabaseConfiguration {
  id: string;
  serviceIds: string[];
  engine: string;
  version: string | null;
  mode: "existing" | "managed";
  name: string;
  storageGiB: number | null;
  connectionSecretName: string | null;
}
export interface DiskConfiguration {
  name: string;
  serviceIds: string[];
  sizeGiB: number;
  storageClass: string | null;
  accessMode: "ReadWriteOnce" | "ReadWriteMany";
}
export interface SecretReference {
  environmentKey: string;
  name: string;
  key: string;
}
export interface IngressConfiguration {
  enabled: boolean;
  host: string | null;
  className: string | null;
  tlsSecretName: string | null;
  path: string;
}
export interface VolumeConfiguration {
  name: string;
  mountPath: string;
  claimName: string | null;
  sizeGiB: number | null;
  storageClass: string | null;
  accessMode: "ReadWriteOnce" | "ReadWriteMany";
}
export interface TerraformInputs {
  kubernetesVersion: string;
  administratorRoleArn: string;
  nodeDiskGiB: number;
  desiredNodes: number;
  privateNetworkEgressVerified: boolean;
  executorPrivateApiReachable: boolean;
}
export interface ClusterCapacity extends ResourceQuantity {
  nodeCount: number;
}
export interface Bindings {
  namespace?: string;
  clusterName?: string | null;
  existingClusterContext?: string | null;
  images?: Array<{ serviceId: string; reference: string }>;
  imagePlatforms?: Record<string, ImagePlatform>;
  secretRefs?: Array<SecretReference & { serviceId: string }>;
  ingress?: Array<IngressConfiguration & { serviceId: string }>;
  databases?: DatabaseConfiguration[];
  volumes?: Array<VolumeConfiguration & { serviceId: string }>;
  terraformInputs?: TerraformInputs | null;
  ingressVerified?: boolean;
  storageDriverVerified?: boolean;
  databasesVerified?: boolean;
  availableCapacity?: ClusterCapacity | null;
  network?: NetworkConfiguration | null;
  runtimeVerified?: boolean;
}
export interface PlanningRequest {
  schemaVersion: "iris.planning-request.v1";
  target: Pick<Target, "stack" | "environment"> &
    Partial<Omit<Target, "stack" | "environment">>;
  constraints?: {
    monthlyBudgetUsd?: number | null;
    expectedRps?: number | null;
    availability?: "single_az" | "multi_az" | null;
  };
  measurements?: Measurement[];
  pricingCatalog?: PricingCatalog | null;
  bindings?: Bindings;
  overrides?: {
    instanceType?: string | null;
    resources?: Array<ResourceAllocation & { serviceId: string }>;
    replicas?: Array<{ serviceId: string; replicas: number }>;
  };
}
export interface Probe {
  kind: "http" | "tcp";
  port: number;
  path: string | null;
  initialDelaySeconds: number;
  periodSeconds: number;
  timeoutSeconds: number;
  failureThreshold: number;
}
export interface Workload {
  serviceId: string;
  name: string;
  /** Registry reference with immutable @sha256 digest, never a floating tag. */
  image: string | null;
  containerPorts: Array<{
    name: string;
    port: number;
    protocol: "TCP" | "UDP";
  }>;
  command: string[] | null;
  args: string[] | null;
  resources: Recommendation<ResourceAllocation>;
  replicas: Recommendation<number>;
  service: Recommendation<{
    type: "ClusterIP";
    port: number;
    targetPort: number;
    protocol: "TCP" | "UDP";
  } | null>;
  ingress: Recommendation<IngressConfiguration>;
  probes: Recommendation<{
    readiness: Probe | null;
    liveness: Probe | null;
    startup: Probe | null;
  }>;
  volumes: Recommendation<VolumeConfiguration[]>;
  secretRefs: Recommendation<SecretReference[]>;
  rollout: Recommendation<{
    strategy: "RollingUpdate";
    maxSurge: number;
    maxUnavailable: number;
    progressDeadlineSeconds: number;
  }>;
  rollback: Recommendation<{
    strategy: "helm_atomic";
    timeoutSeconds: number;
    revisionHistoryLimit: number;
  }>;
}
export interface DeploymentPlan {
  schemaVersion: "iris.deployment-plan.v1";
  /** Canonical SHA256 over every plan field except planDigest. */
  planDigest: string;
  requestDigest: string;
  analysisDigest: string;
  source: {
    sourceSnapshotId: string;
    contextHash: string;
    analysisStatus: "complete" | "needs_input" | "unsupported";
  };
  request: PlanningRequest;
  adapter: StackAdapter | null;
  status: PlanningStatus;
  plannerMode: "policy" | "ai";
  /** True permits fixed-template compilation. Deployment authorization is separate. */
  executionEligible: boolean;
  deploymentAuthorized: false;
  assumptions: Array<{ id: string; description: string; impact: string }>;
  questions: Array<{
    id: string;
    field: string;
    reason: string;
    requiredForExecution: boolean;
  }>;
  recommendations: {
    operatingPolicy?: Recommendation<{
      expectedRps: number;
      availability: "single_az" | "multi_az";
      userMonthlyBudgetUsd: number | null;
      suggestedMonthlyBudgetUsd: number | null;
      budgetConfidence: "provisional" | "measured";
      knownMonthlyCostFloorUsd: number;
      kubernetesVersion?: string | null;
    }>;
    target: Recommendation<Target & { stack: string }>;
    instance: Recommendation<{
      instanceType: string;
      cpuMillicores: number;
      memoryMiB: number;
      minNodes: number;
      maxNodes: number;
    } | null>;
    cost: Recommendation<{
      currency: "USD";
      monthlyTotalUsd: number | null;
      coverage: "complete" | "partial" | "unknown";
      lineItems: Array<{
        key: string;
        monthlyUsd: number | null;
        reason: string;
        catalogItemKey?: string | null;
        quantity?: number | null;
      }>;
    }>;
    network: Recommendation<NetworkConfiguration>;
    databases: Recommendation<DatabaseConfiguration[]>;
    storage: Recommendation<DiskConfiguration[]>;
    /** Optional extension; disabled in the current Kubernetes planning scope. */
    observability: Recommendation<{
      prometheusEnabled: boolean;
      scrapeIntervalSeconds: number;
    }>;
  };
  configuration: {
    namespace: string;
    clusterName: string | null;
    existingClusterContext: string | null;
    imagePlatforms: Record<string, ImagePlatform>;
    terraformInputs: TerraformInputs | null;
    ingressVerified: boolean;
    storageDriverVerified: boolean;
    databasesVerified: boolean;
    availableCapacity: ClusterCapacity | null;
    runtimeVerified: boolean;
    workloads: Workload[];
  };
}
