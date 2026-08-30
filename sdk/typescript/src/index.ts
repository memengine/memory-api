export type MemoryCategory =
  | "preference"
  | "fact"
  | "goal"
  | "procedure"
  | "relationship"
  | "expertise";

export type MessageRole = "user" | "assistant" | "system";

export type RetrievalFeedbackOutcome =
  | "used_successfully"
  | "used_partially"
  | "ignored"
  | "not_useful"
  | "user_corrected"
  | "clarification_needed";

export interface ConversationMessage {
  role: MessageRole;
  content: string;
}

export interface EvidenceReference {
  sourceType: string;
  reference: string;
  contentHash?: string;
}

export interface MemorySource {
  eventId: string;
  service: string;
  observedAt: string;
  scope?: Record<string, unknown>;
  evidence?: EvidenceReference[];
}

export interface MemorySourceOptions {
  eventId?: string;
  observedAt?: string | Date;
  scope?: Record<string, unknown>;
  evidence?: EvidenceReference[];
}

export interface AddRequest {
  externalUserId: string;
  agentId?: string;
  messages: ConversationMessage[];
  metadata?: Record<string, unknown>;
  source?: MemorySource;
}

export interface AddResult {
  jobId: string | null;
  status: string;
  blockedReason?: string | null;
  retryAfterSeconds?: number | null;
  budgetRemainingPct?: number | null;
  quotaMode: "FULL" | "PASSTHROUGH" | "DEGRADED_RETRIEVE" | "BLOCKED";
  processingEtaSeconds: number | null;
  processingStatus: "normal" | "delayed";
  circuitStatus: "HEALTHY" | "DEGRADED" | "CRITICAL";
  nothingToExtract: boolean;
  readonly wasStored: boolean;
}

export interface MemoryJobStatus {
  jobId: string;
  status: string;
  memoriesCreated: number;
  pendingCandidatesBuffered: number;
  pendingCandidatesPromoted: number;
  attempts: number;
  createdAt: string | null;
  processingStartedAt: string | null;
  queueName: string | null;
  error: string | null;
  errorSummary: string | null;
  queuedAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  deadLetteredAt: string | null;
  extractionMetadata: Record<string, unknown>;
  readonly succeeded: boolean;
}

export interface MemoryItem {
  id: string;
  content: string;
  category: MemoryCategory;
  importanceScore: number;
  lastAccessed: string | null;
  relevanceScore: number;
  contextSnippet: string;
  accessCount: number;
  originalImportanceScore: number;
  isHot: boolean;
  systemArchived: boolean;
  sourceEventId: string | null;
  provenance: Record<string, unknown> | null;
  readonly importanceDelta: number;
  readonly importanceTrend: "rising" | "stable" | "decaying";
}

export interface RetrieveResult {
  retrievalId: string | null;
  items: MemoryItem[];
  cached: boolean;
  systemPromptAddition: string;
  contextTokenCount: number;
  memoriesFromHotTier: number;
  quotaMode: "FULL" | "PASSTHROUGH" | "DEGRADED_RETRIEVE" | "BLOCKED";
  isPassthrough: boolean;
  isDegraded: boolean;
  circuitStatus: "HEALTHY" | "DEGRADED" | "CRITICAL";
  clarificationQuestion: string | null;
  readonly hasContext: boolean;
}

export interface RetrievalFeedbackParams {
  retrievalId: string;
  outcome: RetrievalFeedbackOutcome;
  usedMemoryIds?: string[];
  correction?: string;
  agentConfidence?: number;
  metadata?: Record<string, unknown>;
}

export interface RetrievalFeedbackResult {
  feedbackId: string;
  retrievalId: string;
  outcome: string;
  correctionJobId: string | null;
  readonly queuedRetrospectiveExtraction: boolean;
}

export interface MemoryRecord {
  id: string;
  content: string;
  category: MemoryCategory;
  importanceScore: number;
  confidenceScore: number;
  createdAt: string | null;
  updatedAt: string | null;
  lastAccessedAt: string | null;
  accessCount: number;
  originalImportanceScore: number;
  isHot: boolean;
  systemArchived: boolean;
  isArchived: boolean;
  agentId: string | null;
  previousVersionId: string | null;
  sourceConversationId: string | null;
  sourceEventId: string | null;
  provenance: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
  readonly importanceDelta: number;
  readonly importanceTrend: "rising" | "stable" | "decaying";
}

export interface MemoryPage {
  items: MemoryRecord[];
  nextCursor: string | null;
  limit: number;
  total: number;
}

export interface ListOptions {
  pageCursor?: string;
  limit?: number;
}

export interface GetParams {
  query: string;
  externalUserId: string;
  limit?: number;
  categories?: MemoryCategory[];
  agentId?: string;
  timeFilterDays?: number;
  format?: "bullets" | "json" | "xml";
  contextMaxTokens?: number;
  asOf?: string;
}

export interface UserProfile {
  id: string;
  externalId: string;
  email: string;
  settings: Record<string, unknown>;
  memoryCount: number;
  storageBytes: number;
}

export interface ApiKey {
  id: string;
  name: string;
  permissions: string[];
  rateLimitPerMinute: number;
  createdAt: string | null;
  lastUsedAt: string | null;
  isActive: boolean;
}

export interface Agent {
  id: string;
  name: string;
  description: string | null;
  memoryScope: "private" | "shared";
  createdAt: string | null;
}

export interface MemoryExport {
  tenantId: string;
  proxyUserId: string;
  memories: Array<{
    id: string;
    content: string;
    category: MemoryCategory;
    importanceScore: number;
    confidence: number;
    isArchived: boolean;
    createdAt: string | null;
    updatedAt: string | null;
    versions: Array<Record<string, unknown>>;
  }>;
}

export interface EdTechMemoryProfile {
  id: string;
  proxyUserId: string;
  tenantId: string;
  gradeLevel: string | null;
  boardOrCurriculum: string | null;
  subjects: Array<Record<string, unknown>>;
  syllabusStage: Record<string, unknown>;
  strongTopics: Array<Record<string, unknown>>;
  weakTopics: Array<Record<string, unknown>>;
  conceptGaps: Array<Record<string, unknown>>;
  misconceptions: Array<Record<string, unknown>>;
  explanationStyle: Record<string, unknown> | null;
  sessionProfile: Record<string, unknown> | null;
  languageProfile: Record<string, unknown> | null;
  peakHours: Record<string, unknown> | null;
  examName: string | null;
  examDate: string | null;
  marksTarget: Record<string, unknown> | null;
  mockScores: Array<Record<string, unknown>>;
  forgettingStages: Record<string, unknown>;
  improvementVelocity: Record<string, unknown>;
  streak: Record<string, unknown> | null;
  lastTopicStudied: string | null;
  schemaVersion: number;
  lastExtractionAt: string | null;
  extractionSourceJobIds: string[];
  createdAt: string | null;
  updatedAt: string | null;
  readonly hasExamContext: boolean;
  readonly hasLearningProfile: boolean;
}

export interface ErrorPayload {
  error: string;
  code: string;
  requestId: string;
  details?: unknown;
}

interface MemoryOSErrorOptions {
  statusCode?: number | undefined;
  code?: string | undefined;
  requestId?: string | undefined;
  details?: unknown;
}

interface AddEnvelope {
  job_id: string | null;
  status: string;
  blocked_reason?: string | null;
  retry_after_seconds?: number | null;
  budget_remaining_pct?: number | null;
  processing_eta_seconds?: number | null;
  processing_status?: "normal" | "delayed";
  nothing_to_extract?: boolean;
}

interface MemoryJobStatusEnvelope {
  data: {
    job_id: string;
    status: string;
    memories_created?: number;
    pending_candidates_buffered?: number;
    pending_candidates_promoted?: number;
    attempts?: number;
    created_at?: string | null;
    processing_started_at?: string | null;
    queue_name?: string | null;
    error?: string | null;
    error_summary?: string | null;
    queued_at?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
    dead_lettered_at?: string | null;
    extraction_metadata?: Record<string, unknown>;
  };
}

interface RetrieveEnvelope {
  retrieval_id?: string | null;
  data: Array<{
    id: string;
    content: string;
    category: MemoryCategory;
    importance_score: number;
    last_accessed: string | null;
    relevance_score: number;
    context_snippet: string;
    access_count?: number;
    original_importance_score?: number;
    is_hot?: boolean;
    system_archived?: boolean;
    source_event_id?: string | null;
    provenance?: Record<string, unknown> | null;
  }>;
  cached: boolean;
  system_prompt_addition: string;
  context_token_count?: number;
  memories_from_hot_tier?: number;
  quota_mode?: "FULL" | "PASSTHROUGH" | "DEGRADED_RETRIEVE" | "BLOCKED";
  clarification_question?: string | null;
}

interface RetrievalFeedbackEnvelope {
  data: {
    feedback_id: string;
    retrieval_id: string;
    outcome: string;
    correction_job_id?: string | null;
  };
}

interface MemoryListEnvelope {
  data: Array<{
    id: string;
    content: string;
    category: MemoryCategory;
    importance_score: number;
    confidence_score: number;
    created_at: string | null;
    updated_at: string | null;
    last_accessed_at: string | null;
    access_count: number;
    original_importance_score?: number;
    is_hot?: boolean;
    system_archived?: boolean;
    is_archived: boolean;
    agent_id: string | null;
    previous_version_id: string | null;
    source_conversation_id: string | null;
    source_event_id?: string | null;
    provenance?: Record<string, unknown> | null;
    metadata: Record<string, unknown>;
  }>;
  pagination: {
    next_cursor: string | null;
    limit: number;
    total: number;
  };
}

interface DeleteEnvelope {
  data: {
    deleted: boolean;
  };
}

interface ExportEnvelope {
  data: {
    tenant_id: string;
    proxy_user_id: string;
    memories: Array<{
      id: string;
      content: string;
      category: MemoryCategory;
      importance_score: number;
      confidence: number;
      is_archived: boolean;
      created_at: string | null;
      updated_at: string | null;
      versions: Array<Record<string, unknown>>;
    }>;
  };
}

interface EdTechProfileEnvelope {
  data: {
    id: string;
    proxy_user_id: string;
    tenant_id: string;
    grade_level?: string | null;
    board_or_curriculum?: string | null;
    subjects?: Array<Record<string, unknown>>;
    syllabus_stage?: Record<string, unknown>;
    strong_topics?: Array<Record<string, unknown>>;
    weak_topics?: Array<Record<string, unknown>>;
    concept_gaps?: Array<Record<string, unknown>>;
    misconceptions?: Array<Record<string, unknown>>;
    explanation_style?: Record<string, unknown> | null;
    session_profile?: Record<string, unknown> | null;
    language_profile?: Record<string, unknown> | null;
    peak_hours?: Record<string, unknown> | null;
    exam_name?: string | null;
    exam_date?: string | null;
    marks_target?: Record<string, unknown> | null;
    mock_scores?: Array<Record<string, unknown>>;
    forgetting_stages?: Record<string, unknown>;
    improvement_velocity?: Record<string, unknown>;
    streak?: Record<string, unknown> | null;
    last_topic_studied?: string | null;
    schema_version?: number;
    last_extraction_at?: string | null;
    extraction_source_job_ids?: string[];
    created_at?: string | null;
    updated_at?: string | null;
  } | null;
}

type FetchLike = typeof fetch;

export class MemoryOSError extends Error {
  readonly statusCode: number | undefined;
  readonly code: string | undefined;
  readonly requestId: string | undefined;
  readonly details: unknown;

  constructor(
    message: string,
    options: MemoryOSErrorOptions = {},
  ) {
    super(message);
    this.name = "MemoryOSError";
    this.statusCode = options.statusCode;
    this.code = options.code;
    this.requestId = options.requestId;
    this.details = options.details;
  }
}

export class AuthError extends MemoryOSError {
  constructor(message: string, options: MemoryOSErrorOptions = {}) {
    super(message, options);
    this.name = "AuthError";
  }
}

export class RateLimitError extends MemoryOSError {
  constructor(message: string, options: MemoryOSErrorOptions = {}) {
    super(message, options);
    this.name = "RateLimitError";
  }
}

export class NotFoundError extends MemoryOSError {
  constructor(message: string, options: MemoryOSErrorOptions = {}) {
    super(message, options);
    this.name = "NotFoundError";
  }
}

function mapSdkError(
  statusCode: number | undefined,
  message: string,
  code?: string,
  requestId?: string,
  details?: unknown,
): MemoryOSError {
  const options = { statusCode, code, requestId, details };
  if (statusCode === 401) {
    return new AuthError(message, options);
  }
  if (statusCode === 404) {
    return new NotFoundError(message, options);
  }
  if (statusCode === 429) {
    return new RateLimitError(message, options);
  }
  return new MemoryOSError(message, options);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomSourceEventId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `sdk-${crypto.randomUUID()}`;
  }
  return `sdk-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function createMemorySource(service: string, options: MemorySourceOptions = {}): MemorySource {
  const observedAt = options.observedAt instanceof Date
    ? options.observedAt.toISOString()
    : options.observedAt ?? new Date().toISOString();

  return {
    eventId: options.eventId ?? randomSourceEventId(),
    service,
    observedAt,
    scope: options.scope ?? {},
    evidence: options.evidence ?? [],
  };
}

function ensureMessages(messages: ConversationMessage[]): ConversationMessage[] {
  if (messages.length === 0) {
    throw new Error("messages must not be empty.");
  }
  return messages.map((message) => {
    const content = message.content.trim();
    if (!content) {
      throw new Error("message content must not be empty.");
    }
    return { role: message.role, content };
  });
}

function importanceTrend(importanceScore: number, originalImportanceScore: number): "rising" | "stable" | "decaying" {
  const delta = Number((importanceScore - originalImportanceScore).toFixed(2));
  if (delta > 0.3) {
    return "rising";
  }
  if (delta < -0.3) {
    return "decaying";
  }
  return "stable";
}

function toMemoryResult(item: RetrieveEnvelope["data"][number]): MemoryItem {
  const originalImportanceScore = item.original_importance_score ?? item.importance_score;
  return {
    id: item.id,
    content: item.content,
    category: item.category,
    importanceScore: item.importance_score,
    lastAccessed: item.last_accessed,
    relevanceScore: item.relevance_score,
    contextSnippet: item.context_snippet,
    accessCount: item.access_count ?? 0,
    originalImportanceScore,
    isHot: item.is_hot ?? false,
    systemArchived: item.system_archived ?? false,
    sourceEventId: item.source_event_id ?? null,
    provenance: item.provenance ?? null,
    get importanceDelta() {
      return Number((this.importanceScore - this.originalImportanceScore).toFixed(2));
    },
    get importanceTrend() {
      return importanceTrend(this.importanceScore, this.originalImportanceScore);
    },
  };
}

function quotaModeFromHeaders(headers: Headers): RetrieveResult["quotaMode"] {
  const raw = (headers.get("X-MemoryOS-Quota-Mode") ?? "FULL").toUpperCase();
  if (raw === "FULL" || raw === "PASSTHROUGH" || raw === "DEGRADED_RETRIEVE" || raw === "BLOCKED") {
    return raw;
  }
  return "FULL";
}

function circuitStatusFromHeaders(headers: Headers): AddResult["circuitStatus"] {
  const raw = (headers.get("X-MemoryOS-Circuit-Status") ?? "HEALTHY").toUpperCase();
  if (raw === "HEALTHY" || raw === "DEGRADED" || raw === "CRITICAL") {
    return raw;
  }
  return "HEALTHY";
}

function processingStatusFromHeaders(headers: Headers): AddResult["processingStatus"] {
  const raw = (headers.get("X-MemoryOS-Processing") ?? "normal").toLowerCase();
  if (raw === "normal" || raw === "delayed") {
    return raw;
  }
  return "normal";
}

function toMemoryRecord(item: MemoryListEnvelope["data"][number]): MemoryRecord {
  const originalImportanceScore = item.original_importance_score ?? item.importance_score;
  return {
    id: item.id,
    content: item.content,
    category: item.category,
    importanceScore: item.importance_score,
    confidenceScore: item.confidence_score,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    lastAccessedAt: item.last_accessed_at,
    accessCount: item.access_count,
    originalImportanceScore,
    isHot: item.is_hot ?? false,
    systemArchived: item.system_archived ?? false,
    isArchived: item.is_archived,
    agentId: item.agent_id,
    previousVersionId: item.previous_version_id,
    sourceConversationId: item.source_conversation_id,
    sourceEventId: item.source_event_id ?? null,
    provenance: item.provenance ?? null,
    metadata: item.metadata,
    get importanceDelta() {
      return Number((this.importanceScore - this.originalImportanceScore).toFixed(2));
    },
    get importanceTrend() {
      return importanceTrend(this.importanceScore, this.originalImportanceScore);
    },
  };
}

function toEdTechProfile(item: NonNullable<EdTechProfileEnvelope["data"]>): EdTechMemoryProfile {
  return {
    id: item.id,
    proxyUserId: item.proxy_user_id,
    tenantId: item.tenant_id,
    gradeLevel: item.grade_level ?? null,
    boardOrCurriculum: item.board_or_curriculum ?? null,
    subjects: item.subjects ?? [],
    syllabusStage: item.syllabus_stage ?? {},
    strongTopics: item.strong_topics ?? [],
    weakTopics: item.weak_topics ?? [],
    conceptGaps: item.concept_gaps ?? [],
    misconceptions: item.misconceptions ?? [],
    explanationStyle: item.explanation_style ?? null,
    sessionProfile: item.session_profile ?? null,
    languageProfile: item.language_profile ?? null,
    peakHours: item.peak_hours ?? null,
    examName: item.exam_name ?? null,
    examDate: item.exam_date ?? null,
    marksTarget: item.marks_target ?? null,
    mockScores: item.mock_scores ?? [],
    forgettingStages: item.forgetting_stages ?? {},
    improvementVelocity: item.improvement_velocity ?? {},
    streak: item.streak ?? null,
    lastTopicStudied: item.last_topic_studied ?? null,
    schemaVersion: item.schema_version ?? 1,
    lastExtractionAt: item.last_extraction_at ?? null,
    extractionSourceJobIds: item.extraction_source_job_ids ?? [],
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
    get hasExamContext() {
      return Boolean(this.examName || this.examDate || this.marksTarget);
    },
    get hasLearningProfile() {
      return Boolean(this.explanationStyle || this.languageProfile || this.sessionProfile);
    },
  };
}

export class MemoryOS {
  static readonly DEFAULT_BASE_URL = "https://api.memoryo.dev";
  static readonly DEFAULT_TIMEOUT = 30_000;
  static readonly MAX_RETRIES = 3;

  static source(service: string, options: MemorySourceOptions = {}): MemorySource {
    return createMemorySource(service, options);
  }

  readonly apiKey: string;
  readonly baseUrl: string;
  readonly timeout: number;
  private readonly fetchImpl: FetchLike;

  constructor(
    apiKey: string,
    baseUrl: string = MemoryOS.DEFAULT_BASE_URL,
    timeout: number = MemoryOS.DEFAULT_TIMEOUT,
    fetchImpl: FetchLike = fetch,
  ) {
    if (!apiKey.trim()) {
      throw new Error("apiKey must not be empty.");
    }
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeout = timeout;
    this.fetchImpl = fetchImpl;
  }

  async add(
    messages: ConversationMessage[],
    externalUserId: string,
    agentId?: string,
    metadata?: Record<string, unknown>,
    source?: MemorySource,
    idempotencyKey?: string,
  ): Promise<AddResult> {
    const payload: AddRequest = {
      externalUserId,
      messages: ensureMessages(messages),
      ...(agentId ? { agentId } : {}),
      ...(metadata ? { metadata } : {}),
      ...(source ? { source } : {}),
    };
    const response = await this.requestResponse("POST", "/v1/memories/add", {
      ...(idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : {}),
      body: JSON.stringify({
        external_user_id: payload.externalUserId,
        ...(payload.agentId ? { agent_id: payload.agentId } : {}),
        messages: payload.messages,
        metadata: payload.metadata ?? {},
        ...(payload.source
          ? {
              source: {
                event_id: payload.source.eventId,
                service: payload.source.service,
                observed_at: payload.source.observedAt,
                scope: payload.source.scope ?? {},
                evidence: (payload.source.evidence ?? []).map((item) => ({
                  source_type: item.sourceType,
                  reference: item.reference,
                  ...(item.contentHash ? { content_hash: item.contentHash } : {}),
                })),
              },
            }
          : {}),
      }),
    });
    const payloadJson = (await this.parseJson(response)) as AddEnvelope;
    return {
      jobId: payloadJson.job_id,
      status: payloadJson.status,
      blockedReason: payloadJson.blocked_reason ?? null,
      retryAfterSeconds: payloadJson.retry_after_seconds ?? null,
      budgetRemainingPct: payloadJson.budget_remaining_pct ?? null,
      quotaMode: quotaModeFromHeaders(response.headers),
      processingEtaSeconds: payloadJson.processing_eta_seconds ?? null,
      processingStatus: payloadJson.processing_status ?? processingStatusFromHeaders(response.headers),
      circuitStatus: circuitStatusFromHeaders(response.headers),
      nothingToExtract: payloadJson.nothing_to_extract ?? false,
      get wasStored() {
        return payloadJson.status === "queued" && !(payloadJson.nothing_to_extract ?? false);
      },
    };
  }

  async get(params: GetParams): Promise<RetrieveResult>;
  async get(
    query: string,
    externalUserId: string,
    limit?: number,
    categories?: MemoryCategory[],
    agentId?: string,
    timeFilterDays?: number,
    format?: "bullets" | "json" | "xml",
    contextMaxTokens?: number,
  ): Promise<RetrieveResult>;
  async get(
    queryOrParams: string | GetParams,
    externalUserId?: string,
    limit: number = 10,
    categories?: MemoryCategory[],
    agentId?: string,
    timeFilterDays?: number,
    format: "bullets" | "json" | "xml" = "bullets",
    contextMaxTokens: number = 500,
  ): Promise<RetrieveResult> {
    const params =
      typeof queryOrParams === "string"
        ? {
            query: queryOrParams,
            externalUserId: externalUserId ?? "",
            limit,
            categories,
            agentId,
            timeFilterDays,
            format,
            contextMaxTokens,
            asOf: undefined,
          }
        : queryOrParams;

    const body: Record<string, unknown> = {
      query: params.query,
      external_user_id: params.externalUserId,
      limit: params.limit ?? 10,
      categories: params.categories ?? [],
      format: params.format ?? "bullets",
      context_max_tokens: params.contextMaxTokens ?? 500,
    };
    if (params.agentId !== undefined) {
      body.agent_id = params.agentId;
    }
    if (params.timeFilterDays !== undefined) {
      body.time_filter_days = params.timeFilterDays;
    }
    if (params.asOf !== undefined) {
      const parsedAsOf = new Date(params.asOf);
      if (Number.isNaN(parsedAsOf.getTime()) || !/(Z|[+-]\d{2}:\d{2})$/.test(params.asOf)) {
        throw new Error("asOf must be a valid ISO 8601 timestamp with a timezone");
      }
      body.as_of = params.asOf;
    }

    const response = await this.requestResponse("POST", "/v1/memories/retrieve", {
      body: JSON.stringify(body),
    });
    const payload = (await this.parseJson(response)) as RetrieveEnvelope;
    const quotaMode = payload.quota_mode ?? quotaModeFromHeaders(response.headers);
    return {
      retrievalId: payload.retrieval_id ?? null,
      items: payload.data.map(toMemoryResult),
      cached: payload.cached,
      systemPromptAddition: payload.system_prompt_addition,
      contextTokenCount: payload.context_token_count ?? 0,
      memoriesFromHotTier: payload.memories_from_hot_tier ?? 0,
      quotaMode,
      isPassthrough: quotaMode === "PASSTHROUGH",
      isDegraded: quotaMode === "DEGRADED_RETRIEVE",
      circuitStatus: circuitStatusFromHeaders(response.headers),
      clarificationQuestion: payload.clarification_question ?? null,
      get hasContext() {
        return Boolean(payload.system_prompt_addition) && quotaMode !== "PASSTHROUGH";
      },
    };
  }

  async getJobStatus(jobId: string): Promise<MemoryJobStatus> {
    if (!jobId.trim()) {
      throw new Error("jobId must not be empty.");
    }
    const response = await this.requestResponse("GET", `/v1/memories/jobs/${encodeURIComponent(jobId)}`);
    const payload = (await this.parseJson(response)) as MemoryJobStatusEnvelope;
    const data = payload.data;
    return {
      jobId: data.job_id,
      status: data.status,
      memoriesCreated: data.memories_created ?? 0,
      pendingCandidatesBuffered: data.pending_candidates_buffered ?? 0,
      pendingCandidatesPromoted: data.pending_candidates_promoted ?? 0,
      attempts: data.attempts ?? 0,
      createdAt: data.created_at ?? null,
      processingStartedAt: data.processing_started_at ?? null,
      queueName: data.queue_name ?? null,
      error: data.error ?? null,
      errorSummary: data.error_summary ?? null,
      queuedAt: data.queued_at ?? null,
      startedAt: data.started_at ?? null,
      completedAt: data.completed_at ?? null,
      deadLetteredAt: data.dead_lettered_at ?? null,
      extractionMetadata: data.extraction_metadata ?? {},
      get succeeded() {
        return data.status === "completed";
      },
    };
  }

  async waitForJob(
    jobId: string,
    options: { timeoutMs?: number; pollIntervalMs?: number } = {},
  ): Promise<MemoryJobStatus> {
    const timeoutMs = options.timeoutMs ?? 120_000;
    const pollIntervalMs = options.pollIntervalMs ?? 1_000;
    if (timeoutMs <= 0 || pollIntervalMs <= 0) {
      throw new Error("timeoutMs and pollIntervalMs must be positive.");
    }
    const deadline = Date.now() + timeoutMs;
    const terminalStatuses = new Set(["completed", "failed", "dead_letter", "dead_lettered", "cancelled"]);
    while (true) {
      const job = await this.getJobStatus(jobId);
      if (terminalStatuses.has(job.status)) {
        return job;
      }
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for memory job ${jobId}.`);
      }
      await sleep(Math.min(pollIntervalMs, Math.max(0, deadline - Date.now())));
    }
  }

  async feedback(params: RetrievalFeedbackParams): Promise<RetrievalFeedbackResult> {
    const response = await this.requestResponse("POST", "/v1/memories/retrieval-feedback", {
      body: JSON.stringify({
        retrieval_id: params.retrievalId,
        outcome: params.outcome,
        used_memory_ids: params.usedMemoryIds ?? [],
        ...(params.correction !== undefined ? { correction: params.correction } : {}),
        ...(params.agentConfidence !== undefined ? { agent_confidence: params.agentConfidence } : {}),
        metadata: params.metadata ?? {},
      }),
    });
    const payload = (await this.parseJson(response)) as RetrievalFeedbackEnvelope;
    return {
      feedbackId: payload.data.feedback_id,
      retrievalId: payload.data.retrieval_id,
      outcome: payload.data.outcome,
      correctionJobId: payload.data.correction_job_id ?? null,
      get queuedRetrospectiveExtraction() {
        return Boolean(payload.data.correction_job_id);
      },
    };
  }

  async delete(memoryId: string, hardDelete?: boolean): Promise<boolean>;
  /** @deprecated externalUserId is not required; deletion is tenant-scoped by memoryId. */
  async delete(memoryId: string, externalUserId: string, hardDelete?: boolean): Promise<boolean>;
  async delete(
    memoryId: string,
    externalUserIdOrHardDelete: string | boolean = false,
    legacyHardDelete: boolean = false,
  ): Promise<boolean> {
    const hardDelete = typeof externalUserIdOrHardDelete === "boolean"
      ? externalUserIdOrHardDelete
      : legacyHardDelete;
    const response = await this.request<DeleteEnvelope>(
      "DELETE",
      `/v1/memories/${encodeURIComponent(memoryId)}?hard_delete=${hardDelete ? "true" : "false"}`,
    );
    return response.data.deleted;
  }

  async list(externalUserId: string, options: ListOptions = {}): Promise<MemoryPage> {
    const limit = options.limit ?? 50;
    const params = new URLSearchParams({ external_user_id: externalUserId, limit: String(limit) });
    if (options.pageCursor) {
      params.set("cursor", options.pageCursor);
    }
    const response = await this.request<MemoryListEnvelope>("GET", `/v1/memories?${params.toString()}`);
    return {
      items: response.data.map(toMemoryRecord),
      nextCursor: response.pagination.next_cursor,
      limit: response.pagination.limit,
      total: response.pagination.total,
    };
  }

  async export(externalUserId: string): Promise<MemoryExport> {
    const response = await this.request<ExportEnvelope>(
      "GET",
      `/v1/users/${encodeURIComponent(externalUserId)}/export`,
    );
    return {
      tenantId: response.data.tenant_id,
      proxyUserId: response.data.proxy_user_id,
      memories: response.data.memories.map((memory) => ({
        id: memory.id,
        content: memory.content,
        category: memory.category,
        importanceScore: memory.importance_score,
        confidence: memory.confidence,
        isArchived: memory.is_archived,
        createdAt: memory.created_at,
        updatedAt: memory.updated_at,
        versions: memory.versions,
      })),
    };
  }

  async getEdTechProfile(externalUserId: string): Promise<EdTechMemoryProfile | null> {
    const params = new URLSearchParams({ external_user_id: externalUserId });
    const response = await this.request<EdTechProfileEnvelope>("GET", `/v1/memories/edtech-profile?${params.toString()}`);
    return response.data ? toEdTechProfile(response.data) : null;
  }

  /**
   * @deprecated Use getEdTechProfile(). This alias is kept for older callers that used different casing.
   */
  async getEdtechProfile(externalUserId: string): Promise<EdTechMemoryProfile | null> {
    return this.getEdTechProfile(externalUserId);
  }

  private async request<T>(method: string, path: string, init: { body?: string; headers?: Record<string, string> } = {}): Promise<T> {
    const response = await this.requestResponse(method, path, init);
    return (await this.parseJson(response)) as T;
  }

  private async requestResponse(method: string, path: string, init: { body?: string; headers?: Record<string, string> } = {}): Promise<Response> {
    let lastError: MemoryOSError | null = null;

    for (let attempt = 0; attempt <= MemoryOS.MAX_RETRIES; attempt += 1) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), this.timeout);

      try {
        const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
          method,
          headers: {
            Authorization: `ApiKey ${this.apiKey}`,
            Accept: "application/json",
            "Content-Type": "application/json",
            ...init.headers,
          },
          ...(init.body !== undefined ? { body: init.body } : {}),
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        if (response.ok) {
          return response;
        }

        const error = await this.errorFromResponse(response);
        if ([429, 500, 502, 503, 504].includes(response.status) && attempt < MemoryOS.MAX_RETRIES) {
          lastError = error;
          await sleep(2 ** attempt * 1000);
          continue;
        }
        throw error;
      } catch (error) {
        clearTimeout(timeoutId);
        if (error instanceof MemoryOSError) {
          throw error;
        }
        if (attempt < MemoryOS.MAX_RETRIES) {
          await sleep(2 ** attempt * 1000);
          continue;
        }
        throw new MemoryOSError(`MemoryOS request failed: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    throw lastError ?? new MemoryOSError("Request failed after retries.");
  }

  private async parseJson(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      throw new MemoryOSError("MemoryOS returned a non-JSON response.", { statusCode: response.status });
    }
  }

  private async errorFromResponse(response: Response): Promise<MemoryOSError> {
    try {
      const payload = (await this.parseJson(response)) as Partial<{
        error: string;
        code: string;
        request_id: string;
        details: unknown;
      }>;
      return mapSdkError(
        response.status,
        (payload.error ?? `request_failed_${response.status}`).replace(/_/g, " "),
        payload.code,
        payload.request_id,
        payload.details,
      );
    } catch (error) {
      if (error instanceof MemoryOSError) {
        return error;
      }
      return mapSdkError(response.status, `MemoryOS request failed with status ${response.status}.`);
    }
  }
}

export { UniversalMemoryOS } from "./universal";
export type { UniversalRetrieveResult } from "./universal";
