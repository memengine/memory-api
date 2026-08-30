import type { AddResult, ConversationMessage, MemoryItem, RetrieveResult } from "./index";
import { AuthError, MemoryOSError, NotFoundError, RateLimitError } from "./index";

export interface UniversalRetrieveResult extends RetrieveResult {
  categoriesAvailable: string[];
  permissionStatus: string | null;
}

interface UniversalRetrieveEnvelope {
  retrieval_id?: string | null;
  data: Array<{
    id: string;
    content: string;
    category: MemoryItem["category"];
    importance_score: number;
    last_accessed: string | null;
    relevance_score: number;
    context_snippet: string;
    source_event_id?: string | null;
    provenance?: Record<string, unknown> | null;
  }>;
  cached: boolean;
  system_prompt_addition: string;
  context_token_count?: number;
  permission_error?: string | null;
  categories_available?: string[];
  is_passthrough?: boolean;
}

type FetchLike = typeof fetch;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function toMemoryItem(item: UniversalRetrieveEnvelope["data"][number]): MemoryItem {
  const originalImportanceScore = item.importance_score;
  return {
    id: item.id,
    content: item.content,
    category: item.category,
    importanceScore: item.importance_score,
    lastAccessed: item.last_accessed,
    relevanceScore: item.relevance_score,
    contextSnippet: item.context_snippet,
    accessCount: 0,
    originalImportanceScore,
    isHot: false,
    systemArchived: false,
    sourceEventId: item.source_event_id ?? null,
    provenance: item.provenance ?? null,
    get importanceDelta() {
      return Number((this.importanceScore - this.originalImportanceScore).toFixed(2));
    },
    get importanceTrend() {
      const delta = this.importanceDelta;
      if (delta > 0.3) {
        return "rising";
      }
      if (delta < -0.3) {
        return "decaying";
      }
      return "stable";
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

export class UniversalMemoryOS {
  static readonly DEFAULT_BASE_URL = "https://api.memoryo.dev";
  static readonly DEFAULT_CONSENT_BASE_URL = "https://consent.memoryo.dev";
  static readonly DEFAULT_TIMEOUT = 30_000;
  static readonly MAX_RETRIES = 3;

  readonly agentApiKey: string;
  readonly uuiToken: string;
  readonly baseUrl: string;
  readonly timeout: number;
  private readonly fetchImpl: FetchLike;

  constructor(
    agentApiKey: string,
    uuiToken: string,
    baseUrl: string = UniversalMemoryOS.DEFAULT_BASE_URL,
    timeout: number = UniversalMemoryOS.DEFAULT_TIMEOUT,
    fetchImpl: FetchLike = fetch,
  ) {
    if (!agentApiKey.trim()) {
      throw new Error("agentApiKey must not be empty.");
    }
    if (!uuiToken.trim()) {
      throw new Error("uuiToken must not be empty.");
    }
    this.agentApiKey = agentApiKey;
    this.uuiToken = uuiToken;
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeout = timeout;
    this.fetchImpl = fetchImpl;
  }

  async add(
    messages: ConversationMessage[],
    metadata?: Record<string, unknown>,
    idempotencyKey?: string,
  ): Promise<AddResult> {
    const response = await this.requestResponse("POST", "/v1/universal/memories/add", {
      body: JSON.stringify({
        messages,
        metadata: metadata ?? {},
        ...(idempotencyKey !== undefined ? { idempotency_key: idempotencyKey } : {}),
      }),
    });
    const payload = (await this.parseJson(response)) as {
      job_id: string | null;
      status: string;
      blocked_reason?: string | null;
      retry_after_seconds?: number | null;
      budget_remaining_pct?: number | null;
      processing_eta_seconds?: number | null;
      processing_status?: "normal" | "delayed";
    };
    return {
      jobId: payload.job_id,
      status: payload.status,
      blockedReason: payload.blocked_reason ?? null,
      retryAfterSeconds: payload.retry_after_seconds ?? null,
      budgetRemainingPct: payload.budget_remaining_pct ?? null,
      quotaMode: quotaModeFromHeaders(response.headers),
      processingEtaSeconds: payload.processing_eta_seconds ?? null,
      processingStatus: payload.processing_status ?? processingStatusFromHeaders(response.headers),
      circuitStatus: circuitStatusFromHeaders(response.headers),
      nothingToExtract: false,
      get wasStored() {
        return payload.status === "queued";
      },
    };
  }

  async get(
    query: string,
    limit = 10,
    options: { format?: "bullets" | "json" | "xml"; contextMaxTokens?: number } = {},
  ): Promise<UniversalRetrieveResult> {
    const response = await this.requestResponse("POST", "/v1/universal/memories/retrieve", {
      body: JSON.stringify({
        query,
        limit,
        format: options.format ?? "bullets",
        context_max_tokens: options.contextMaxTokens ?? 500,
      }),
    });
    const payload = (await this.parseJson(response)) as UniversalRetrieveEnvelope;
    const quotaMode = quotaModeFromHeaders(response.headers);
    return {
      retrievalId: payload.retrieval_id ?? null,
      items: payload.data.map(toMemoryItem),
      cached: payload.cached,
      systemPromptAddition: payload.system_prompt_addition,
      quotaMode,
      isPassthrough: Boolean(payload.is_passthrough) || quotaMode === "PASSTHROUGH",
      isDegraded: quotaMode === "DEGRADED_RETRIEVE",
      circuitStatus: circuitStatusFromHeaders(response.headers),
      clarificationQuestion: null,
      contextTokenCount: payload.context_token_count ?? 0,
      memoriesFromHotTier: 0,
      get hasContext() {
        return Boolean(payload.system_prompt_addition) && !(Boolean(payload.is_passthrough) || quotaMode === "PASSTHROUGH");
      },
      categoriesAvailable: payload.categories_available ?? [],
      permissionStatus: payload.permission_error ?? null,
    };
  }

  static consentUrl(agentId: string, redirectUri?: string | null, state?: string, categories?: string[]): string {
    if (!agentId.trim()) {
      throw new Error("agentId must not be empty.");
    }

    const params = new URLSearchParams({ agent_id: agentId });
    if (redirectUri?.trim()) {
      params.set("redirect_uri", redirectUri);
    }
    if (state) {
      params.set("state", state);
    }
    const cleanedCategories = Array.from(new Set((categories ?? []).map((category) => category.trim()).filter(Boolean)));
    if (cleanedCategories.length > 0) {
      params.set("categories", cleanedCategories.join(","));
    }
    return `${UniversalMemoryOS.DEFAULT_CONSENT_BASE_URL}/consent?${params.toString()}`;
  }

  private async requestResponse(method: string, path: string, init: { body?: string } = {}): Promise<Response> {
    let lastError: MemoryOSError | null = null;

    for (let attempt = 0; attempt <= UniversalMemoryOS.MAX_RETRIES; attempt += 1) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), this.timeout);

      try {
        const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
          method,
          headers: {
            Authorization: `ApiKey ${this.agentApiKey}`,
            "X-MemoryOS-UUI": this.uuiToken,
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
        if ([429, 500, 502, 503, 504].includes(response.status) && attempt < UniversalMemoryOS.MAX_RETRIES) {
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
        if (attempt < UniversalMemoryOS.MAX_RETRIES) {
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
