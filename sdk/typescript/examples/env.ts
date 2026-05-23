declare const process:
  | {
      env?: Record<string, string | undefined>;
    }
  | undefined;

export function memoryOSApiKey(): string {
  return process?.env?.MEMORYOS_API_KEY ?? "";
}

export function memoryOSBaseUrl(): string {
  return process?.env?.MEMORYOS_BASE_URL ?? "http://127.0.0.1:8000";
}
