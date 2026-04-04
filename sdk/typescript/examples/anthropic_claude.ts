import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS("mem_live_xxx", "http://127.0.0.1:8000");
  const memories = await client.get("customer support style", "student_44821", 4);
  const xmlBlock = memories.quotaMode === "PASSTHROUGH"
    ? "<memories />"
    : memories.systemPromptAddition;
  console.log("Claude system prompt context:");
  console.log(xmlBlock);
}

void main();
