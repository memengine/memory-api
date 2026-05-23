import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());
  const memories = await client.get({
    query: "customer support style",
    externalUserId: "student_44821",
    limit: 4,
    format: "xml",
    contextMaxTokens: 300,
  });
  const xmlBlock = memories.hasContext ? memories.systemPromptAddition : "<memories />";
  console.log("Claude system prompt context:");
  console.log(xmlBlock);
}

void main();
