import { memoryOSApiKey, memoryOSBaseUrl } from "./env.js";
import { MemoryOS } from "../src/index.js";

async function main(): Promise<void> {
  const client = new MemoryOS(memoryOSApiKey(), memoryOSBaseUrl());

  const profile = await client.getEdTechProfile("student_44821");
  if (!profile) {
    console.log("No EdTech profile yet. Add a student conversation first.");
    return;
  }

  console.log("Student:", profile.gradeLevel ?? "unknown grade");
  if (profile.hasExamContext) {
    console.log("Exam:", profile.examName, profile.examDate);
  }
  if (profile.hasLearningProfile) {
    console.log("Learning style:", profile.explanationStyle);
  }

  console.log("Top weak topics:");
  for (const topic of profile.weakTopics.slice(0, 3)) {
    console.log("-", topic);
  }
}

void main();
