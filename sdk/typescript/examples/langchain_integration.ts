import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS("mem_live_xxx", "http://127.0.0.1:8000");
  const results = await client.get("programming language preferences", "student_44821", 3);
  const context = results.quotaMode === "PASSTHROUGH" ? "" : results.systemPromptAddition;
  console.log("Inject this into your LangChain prompt:");
  console.log(context);
}

void main();
