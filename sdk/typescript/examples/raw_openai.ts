import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS("mem_live_xxx", "http://127.0.0.1:8000");
  const memories = await client.get("product preferences", "student_44821", 5);
  const promptAddition = memories.quotaMode === "PASSTHROUGH" ? "" : memories.systemPromptAddition;
  console.log("Use this before your LLM chat request:");
  console.log(promptAddition);
}

void main();
