import { MemoryOS } from "../src/index.js";

const memoryClient = new MemoryOS("mem_live_xxx", "http://127.0.0.1:8000");

export async function assistantContext(query: string, externalUserId: string): Promise<{ quotaMode: string; systemPromptAddition: string; memories: string[] }> {
  const results = await memoryClient.get(query, externalUserId, 5);
  return {
    quotaMode: results.quotaMode,
    systemPromptAddition: results.systemPromptAddition,
    memories: results.items.map((item) => item.content),
  };
}
