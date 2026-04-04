export type MemoryCategory =
  | "preference"
  | "fact"
  | "goal"
  | "procedure"
  | "relationship"
  | "expertise";

export type MessageRole = "user" | "assistant" | "system";

export interface ConversationMessage {
  role: MessageRole;
  content: string;
}

export interface AddRequest {
  externalUserId: string;
  agentId?: string;
  messages: ConversationMessage[];
  metadata?: Record<string, unknown>;
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
}

export interface MemoryItem {
  id: string;
  content: string;
  category: MemoryCategory;
  importanceScore: number;
  lastAccessed: string | null;
  relevanceScore: number;
  contextSnippet: string;
}

export interface RetrieveResult {
  items: MemoryItem[];
  cached: boolean;
  systemPromptAddition: string;
  quotaMode: "FULL" | "PASSTHROUGH" | "DEGRADED_RETRIEVE" | "BLOCKED";
  isPassthrough: boolean;
  isDegraded: boolean;
  circuitStatus: "HEALTHY" | "DEGRADED" | "CRITICAL";
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
  isArchived: boolean;
  agentId: string | null;
  previousVersionId: string | null;
  sourceConversationId: string | null;
  metadata: Record<string, unknown>;
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
  user: UserProfile;
  memories: MemoryRecord[];
  apiKeys: ApiKey[];
  agents: Agent[];
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
}

interface RetrieveEnvelope {
  data: Array<{
    id: string;
    content: string;
    category: MemoryCategory;
    importance_score: number;
    last_accessed: string | null;
    relevance_score: number;
    context_snippet: string;
  }>;
  cached: boolean;
  system_prompt_addition: string;
  quota_mode?: "FULL" | "PASSTHROUGH" | "DEGRADED_RETRIEVE" | "BLOCKED";
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
    is_archived: boolean;
    agent_id: string | null;
    previous_version_id: string | null;
    source_conversation_id: string | null;
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
    user: {
      id: string;
      external_id: string;
      email: string;
      settings: Record<string, unknown>;
      memory_count: number;
      storage_bytes: number;
    };
    memories: MemoryListEnvelope["data"];
    api_keys: Array<{
      id: string;
      name: string;
      permissions: string[];
      rate_limit_per_minute: number;
      created_at: string | null;
      last_used_at: string | null;
      is_active: boolean;
    }>;
    agents: Array<{
      id: string;
      name: string;
      description: string | null;
      memory_scope: "private" | "shared";
      created_at: string | null;
    }>;
  };
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

function toMemoryResult(item: RetrieveEnvelope["data"][number]): MemoryItem {
  return {
    id: item.id,
    content: item.content,
    category: item.category,
    importanceScore: item.importance_score,
    lastAccessed: item.last_accessed,
    relevanceScore: item.relevance_score,
    contextSnippet: item.context_snippet,
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
    isArchived: item.is_archived,
    agentId: item.agent_id,
    previousVersionId: item.previous_version_id,
    sourceConversationId: item.source_conversation_id,
    metadata: item.metadata,
  };
}

function toUserProfile(user: ExportEnvelope["data"]["user"]): UserProfile {
  return {
    id: user.id,
    externalId: user.external_id,
    email: user.email,
    settings: user.settings,
    memoryCount: user.memory_count,
    storageBytes: user.storage_bytes,
  };
}

function toApiKey(item: ExportEnvelope["data"]["api_keys"][number]): ApiKey {
  return {
    id: item.id,
    name: item.name,
    permissions: item.permissions,
    rateLimitPerMinute: item.rate_limit_per_minute,
    createdAt: item.created_at,
    lastUsedAt: item.last_used_at,
    isActive: item.is_active,
  };
}

function toAgent(item: ExportEnvelope["data"]["agents"][number]): Agent {
  return {
    id: item.id,
    name: item.name,
    description: item.description,
    memoryScope: item.memory_scope,
    createdAt: item.created_at,
  };
}

export class MemoryOS {
  static readonly DEFAULT_BASE_URL = "https://api.memoryos.io";
  static readonly DEFAULT_TIMEOUT = 30_000;
  static readonly MAX_RETRIES = 3;

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
  ): Promise<AddResult> {
    const payload: AddRequest = {
      externalUserId,
      messages: ensureMessages(messages),
      ...(agentId ? { agentId } : {}),
      ...(metadata ? { metadata } : {}),
    };
    const response = await this.requestResponse("POST", "/v1/memories/add", {
      body: JSON.stringify({
        external_user_id: payload.externalUserId,
        ...(payload.agentId ? { agent_id: payload.agentId } : {}),
        messages: payload.messages,
        metadata: payload.metadata ?? {},
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
    };
  }

  async get(
    query: string,
    externalUserId: string,
    limit: number = 10,
    categories?: MemoryCategory[],
  ): Promise<RetrieveResult> {
    const response = await this.requestResponse("POST", "/v1/memories/retrieve", {
      body: JSON.stringify({
        query,
        external_user_id: externalUserId,
        limit,
        categories: categories ?? [],
        format: "bullets",
      }),
    });
    const payload = (await this.parseJson(response)) as RetrieveEnvelope;
    const quotaMode = payload.quota_mode ?? quotaModeFromHeaders(response.headers);
    return {
      items: payload.data.map(toMemoryResult),
      cached: payload.cached,
      systemPromptAddition: payload.system_prompt_addition,
      quotaMode,
      isPassthrough: quotaMode === "PASSTHROUGH",
      isDegraded: quotaMode === "DEGRADED_RETRIEVE",
      circuitStatus: circuitStatusFromHeaders(response.headers),
    };
  }

  async delete(memoryId: string, externalUserId: string, hardDelete: boolean = false): Promise<boolean> {
    void externalUserId;
    const response = await this.request<DeleteEnvelope>(
      "DELETE",
      `/v1/memories/${encodeURIComponent(memoryId)}?hard_delete=${hardDelete ? "true" : "false"}`,
    );
    return response.data.deleted;
  }

  async list(externalUserId: string, options: ListOptions = {}): Promise<MemoryPage> {
    void externalUserId;
    const limit = options.limit ?? 50;
    const params = new URLSearchParams({ limit: String(limit) });
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
    void externalUserId;
    const response = await this.request<ExportEnvelope>("GET", "/v1/users/me/export");
    return {
      user: toUserProfile(response.data.user),
      memories: response.data.memories.map(toMemoryRecord),
      apiKeys: response.data.api_keys.map(toApiKey),
      agents: response.data.agents.map(toAgent),
    };
  }

  private async request<T>(method: string, path: string, init: { body?: string } = {}): Promise<T> {
    const response = await this.requestResponse(method, path, init);
    return (await this.parseJson(response)) as T;
  }

  private async requestResponse(method: string, path: string, init: { body?: string } = {}): Promise<Response> {
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
