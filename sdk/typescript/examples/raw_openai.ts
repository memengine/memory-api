import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());
  const externalUserId = "student_44821";

  await client.add(
    [
      { role: "user", content: "I prefer concise answers with TypeScript examples." },
      { role: "assistant", content: "Got it, I will keep examples TypeScript-first." },
    ],
    externalUserId,
  );

  const memories = await client.get({
    query: "how should I explain this coding concept?",
    externalUserId,
    limit: 5,
    contextMaxTokens: 300,
  });
  const promptAddition = memories.hasContext ? memories.systemPromptAddition : "";
  console.log("Prepend this to your OpenAI system prompt:");
  console.log(promptAddition);
}

void main();
