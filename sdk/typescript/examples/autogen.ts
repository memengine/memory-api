import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS("mem_live_xxx", "http://127.0.0.1:8000");
  const results = await client.get("coding preferences", "student_44821", 3);
  console.log("AutoGen agent memory context:");
  console.log(results.items.map((item) => item.content));
}

void main();
