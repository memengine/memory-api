import type { AddResult, ConversationMessage, MemoryItem, RetrieveResult } from "./index";
import { AuthError, MemoryOSError, NotFoundError, RateLimitError } from "./index";

export interface UniversalRetrieveResult extends RetrieveResult {
  categoriesAvailable: string[];
  permissionStatus: string | null;
}

interface UniversalRetrieveEnvelope {
  data: Array<{
    id: string;
    content: string;
    category: MemoryItem["category"];
    importance_score: number;
    last_accessed: string | null;
    relevance_score: number;
    context_snippet: string;
  }>;
  cached: boolean;
  system_prompt_addition: string;
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

export class UniversalMemoryOS {
  static readonly DEFAULT_BASE_URL = "https://api.memoryos.io";
  static readonly DEFAULT_CONSENT_BASE_URL = "https://consent.memoryos.io";
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

  async add(messages: ConversationMessage[], metadata?: Record<string, unknown>): Promise<AddResult> {
    const response = await this.requestResponse("POST", "/v1/universal/memories/add", {
      body: JSON.stringify({
        messages,
        metadata: metadata ?? {},
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
    };
  }

  async get(query: string, limit = 10): Promise<UniversalRetrieveResult> {
    const response = await this.requestResponse("POST", "/v1/universal/memories/retrieve", {
      body: JSON.stringify({
        query,
        limit,
        format: "bullets",
      }),
    });
    const payload = (await this.parseJson(response)) as UniversalRetrieveEnvelope;
    const quotaMode = quotaModeFromHeaders(response.headers);
    return {
      items: payload.data.map(toMemoryItem),
      cached: payload.cached,
      systemPromptAddition: payload.system_prompt_addition,
      quotaMode,
      isPassthrough: Boolean(payload.is_passthrough) || quotaMode === "PASSTHROUGH",
      isDegraded: quotaMode === "DEGRADED_RETRIEVE",
      circuitStatus: circuitStatusFromHeaders(response.headers),
      categoriesAvailable: payload.categories_available ?? [],
      permissionStatus: payload.permission_error ?? null,
    };
  }

  static consentUrl(agentId: string, redirectUri: string, state?: string): string {
    if (!agentId.trim()) {
      throw new Error("agentId must not be empty.");
    }
    if (!redirectUri.trim()) {
      throw new Error("redirectUri must not be empty.");
    }

    const params = new URLSearchParams({
      agent_id: agentId,
      redirect_uri: redirectUri,
    });
    if (state) {
      params.set("state", state);
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
