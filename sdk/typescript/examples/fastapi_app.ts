import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

const memoryClient = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());

export async function assistantContext(query: string, externalUserId: string): Promise<{
  quotaMode: string;
  systemPromptAddition: string;
  contextTokenCount: number;
  memories: string[];
}> {
  const results = await memoryClient.get({
    query,
    externalUserId,
    limit: 5,
    contextMaxTokens: 300,
  });
  return {
    quotaMode: results.quotaMode,
    systemPromptAddition: results.hasContext ? results.systemPromptAddition : "",
    contextTokenCount: results.contextTokenCount,
    memories: results.items.map((item) => item.content),
  };
}
