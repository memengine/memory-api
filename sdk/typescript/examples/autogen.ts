import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());
  const results = await client.get({
    query: "coding preferences",
    externalUserId: "student_44821",
    limit: 3,
  });
  console.log("AutoGen agent memory context:");
  console.log(results.hasContext ? results.systemPromptAddition : "");
}

void main();
