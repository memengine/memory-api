import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());
  const results = await client.get({
    query: "programming language preferences",
    externalUserId: "student_44821",
    limit: 3,
    timeFilterDays: 30,
  });
  const context = results.hasContext ? results.systemPromptAddition : "";
  console.log("Inject this into your LangChain prompt:");
  console.log(context);
}

void main();
