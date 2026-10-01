/** Draft data contract for the reviewed iris-was ApiResponse boundary.
 * No public URL, queue kind or deployment approval action is specified here.
 */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };
export type AnalysisStatus = "complete" | "needs_input" | "unsupported";
export type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "RETRY_WAIT"
  | "FAILED"
  | "MANUAL_INTERVENTION";

interface FieldBase {
  scope: string;
  evidenceIds: string[];
  reason: string;
}
export type AnalysisField<T = JsonValue> = FieldBase &
  (
    | { status: "unknown"; value: null }
    | { status: "detected" | "suggested"; value: T }
  );

export interface AnalysisService {
  serviceId: string;
  root: AnalysisField;
  role: AnalysisField;
  runtime: AnalysisField;
  buildCommand: AnalysisField;
  startCommand: AnalysisField;
  outputDirectory: AnalysisField;
  workingDirectory?: AnalysisField;
  ports: AnalysisField[];
  healthchecks: AnalysisField[];
  componentRoots: string[];
}

export interface AnalysisResult {
  schemaVersion: "1";
  status: AnalysisStatus;
  sourceSnapshotId: string;
  contextHash: string;
  services: AnalysisService[];
  dependencies: AnalysisField[];
  apiRoutes: AnalysisField[];
  environmentKeys: AnalysisField[];
  connections: AnalysisField[];
  questions: Array<{
    key: string;
    reason: string;
    kind: "code_review" | "user_configuration";
  }>;
  coverage: { completeForProfile: boolean; limitations: string[] };
}

export interface AnalysisResponseData {
  contractVersion: "iris.control-plane.analysis.v1-draft";
  /** Analysis completed; this does not complete a containing BUILD/DEPLOY job. */
  analysisExecutionStatus: "SUCCEEDED";
  analysisStatus: AnalysisStatus;
  analysisMode: "static" | "opencode";
  deploymentAuthorized: false;
  reviewRequired: boolean;
  sourceSnapshotId: string;
  contextHash: string;
  analysisResult: AnalysisResult;
}

export interface ApiResponse<T> {
  success: boolean;
  code?: string;
  message?: string;
  data?: T;
  details?: Array<{ field: string; reason: string }>;
}
// X-Request-ID is an HTTP response header; it is deliberately absent from data.
