/** Standalone Organization agent v1. No WAS route/queue/database changes. */
export interface OrganizationRequest {
  schemaVersion: "iris.organization-request.v1";
  organization: string;
  purpose?: string | null;
  environment?: "preview" | "development" | "production";
  includeRepositories?: string[];
  excludeRepositories?: string[];
  refs?: Record<string, string>;
  maxRepositories?: number;
  selectedServiceIds?: string[];
  target?: {
    kind?: "existing_kubernetes" | "aws_eks";
    context?: string | null;
    namespace?: string;
    architecture?: "amd64" | "arm64";
  };
  serviceBindings?: Array<{
    serviceId: string;
    builder?: "dockerfile" | "railpack";
    buildContext?: string;
    dockerfilePath?: string | null;
    buildCommand?: string | null;
    startCommand?: string[] | string | null;
    imageRepository?: string;
    imageReference?: string;
    port?: number;
    publicHost?: string;
    endpoint?: string;
    replicas?: number;
    runAsUser?: number;
    resources?: {
      requests: { cpuMillicores: number; memoryMiB: number };
      limits: { cpuMillicores: number; memoryMiB: number };
    };
    runtimeEnv?: Array<{ key: string; value: string }>;
    buildEnv?: Array<{ key: string; value: string }>;
    secretRefs?: Array<{ key: string; name: string; secretKey: string }>;
  }>;
  connectionBindings?: Array<{
    fromServiceId: string;
    toServiceId: string;
    kind: "http" | "database" | "queue" | "storage" | "package";
    environmentKey?: string | null;
    phase?: "build" | "runtime" | "unknown";
  }>;
  deploymentRequest?: Record<string, unknown> | null;
  httpChecks?: Array<{ url: string; expectedStatus?: number; timeoutSeconds?: number }>;
}

export interface OrganizationSources {
  schemaVersion: "iris.organization-sources.v1";
  organization: string;
  repositories: Array<{
    repositoryId: string;
    fullName: string;
    commitSha: string;
    ref?: string;
    sourceRoot: string; // CLI resolves relative to the input manifest; library requires absolute.
  }>;
}

export interface SystemAuthorization {
  planDigest: string;
  bundleDigest: string;
  context: string;
  namespace: string;
  allowDeploy: boolean;
  allowBuilds?: boolean;
  allowPushes?: boolean;
  approvedHttpUrls?: string[];
}

export interface SystemQuestion {
  key: string;
  reason: string;
  serviceId: string | null;
  requiredForExecution: boolean;
}

export interface OrganizationResult {
  schemaVersion: "iris.organization-result.v1";
  organization: string;
  snapshotDigest: string;
  deploymentAuthorized: false;
  graph: Record<string, unknown> & { schemaVersion: "iris.system-graph.v1"; graphDigest: string };
  plan: Record<string, unknown> & {
    schemaVersion: "iris.system-plan.v1";
    planDigest: string;
    status: "needs_input" | "build_required" | "ready";
    executionAuthorized: false;
    executionEligible: boolean;
    buildEligible: boolean;
    questions: SystemQuestion[];
  };
  systemAdvice?: Record<string, unknown>;
  advisorReport?: Record<string, unknown>;
}
