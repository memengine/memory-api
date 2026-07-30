# MemoryOS Example Extraction Spec

This is a development-safe extraction spec for contributors. It is not the production policy, but it is complete enough to run local extraction, tests, and demos.

## 1. Memory Categories

### EXPERTISE
**Definition:**
Skills, knowledge areas, tools, languages, frameworks, or subjects the user knows, is learning, or uses regularly.

Examples:

- User has three years of FastAPI experience.
- User is learning Rust for systems programming.
- User understands React hooks but struggles with server components.

---

### PREFERENCE
**Definition:**
Stable preferences about how the user likes answers, tools, workflows, formats, tone, or product behaviour.

Examples:

- User prefers concise technical answers with Python examples.
- User likes visual explanations before code.
- User prefers dark mode and keyboard shortcuts.

---

### GOAL
**Definition:**
User objectives, plans, targets, or desired outcomes that may shape future help.

Examples:

- User wants to build a B2B SaaS product.
- User is preparing for cloud certification.
- User wants to improve public speaking.

---

### FACT
**Definition:**
Stable factual information about the user, their role, context, location, project, or current situation.

Examples:

- User works remotely from Bangalore.
- User is a backend engineer at a fintech startup.
- User is currently using PostgreSQL as the primary database.

---

### PROCEDURE
**Definition:**
How the user does something, including workflows, habits, steps, recurring processes, or operating preferences.

Examples:

- User deploys through GitHub Actions to AWS ECS.
- User reviews pull requests every morning.
- User writes tests before refactoring service code.

---

### RELATIONSHIP
**Definition:**
People, teams, organisations, collaborators, customers, or relationships that may matter in future context.

Examples:

- User has a technical co-founder named Raj.
- User works with the platform team on deployment issues.
- User mentors junior developers on FastAPI.

---

## 2. Importance Scoring Rubric

Score memories from 1 to 10 based on future usefulness.

- 1: Trivial or probably useless. Example: "User said thanks."
- 2: Very low value. Example: "User asked one temporary question."
- 3: Mildly useful context. Example: "User is trying a new editor today."
- 4: Useful but not central. Example: "User sometimes uses Docker Compose."
- 5: Moderately useful recurring context. Example: "User writes backend APIs."
- 6: Strong useful context. Example: "User prefers Python examples."
- 7: Important personalisation signal. Example: "User is learning FastAPI for production work."
- 8: Highly important long-term context. Example: "User is building a B2B SaaS for Indian SMBs."
- 9: Critical context for many future answers. Example: "User is migrating their production system from Django to FastAPI."
- 10: Essential, stable, and broadly applicable. Use rarely.

Prefer 5-8 for most useful memories. Avoid high scores for short-lived or uncertain information.

## 3. Example Conversations

### Example 1: Developer Preference

Conversation:

```text
[user]: I prefer concise answers with Python examples. Long theory slows me down.
[assistant]: Got it.
```

Expected:

```json
{
  "memories": [
    {
      "content": "User prefers concise technical answers with Python examples.",
      "category": "preference",
      "importance_score": 7.5,
      "confidence": 0.95,
      "reasoning": "The user explicitly stated a stable answer-format preference."
    }
  ],
  "nothing_to_extract": false,
  "extraction_notes": "Preference is reusable."
}
```

### Example 2: Student Goal

Conversation:

```text
[user]: I am preparing for my AWS certification exam next month.
[assistant]: I can help you make a study plan.
```

Expected:

```json
{
  "memories": [
    {
      "content": "User is preparing for an AWS certification exam next month.",
      "category": "goal",
      "importance_score": 7.0,
      "confidence": 0.9,
      "reasoning": "The user described a near-term learning goal."
    }
  ],
  "nothing_to_extract": false
}
```

### Example 3: Professional Context

Conversation:

```text
[user]: At work I maintain a FastAPI service backed by PostgreSQL and Redis.
[assistant]: That stack is common for API-heavy products.
```

Expected:

```json
{
  "memories": [
    {
      "content": "User maintains a FastAPI service backed by PostgreSQL and Redis at work.",
      "category": "fact",
      "importance_score": 7.2,
      "confidence": 0.9,
      "reasoning": "The user's work stack is likely useful for future technical help."
    }
  ],
  "nothing_to_extract": false
}
```

### Example 4: Greeting Only

Conversation:

```text
[user]: hi
[assistant]: Hello! How can I help?
[user]: thanks
```

Expected:

```json
{
  "memories": [],
  "nothing_to_extract": true,
  "extraction_notes": "The conversation contains no reusable user information."
}
```

### Example 5: Conflict Scenario

Conversation:

```text
[user]: Earlier I said my exam was in March, but it has been postponed to April.
[assistant]: Thanks for the update.
```

Expected:

```json
{
  "memories": [
    {
      "content": "User's exam has been postponed to April.",
      "category": "fact",
      "importance_score": 7.0,
      "confidence": 0.92,
      "reasoning": "The user corrected a previous date-related fact."
    }
  ],
  "nothing_to_extract": false,
  "extraction_notes": "This may supersede an older exam date memory."
}
```

## 4. What Should NEVER Be Stored

**Rule 1 Secrets and credentials**
Never store API keys, passwords, tokens, private keys, seed phrases, or authentication codes.

---

**Rule 2 Payment data**
Never store full card numbers, bank account numbers, CVVs, or payment credentials.

---

**Rule 3 Highly sensitive identity data**
Never store government ID numbers, passport numbers, or tax IDs.

---

**Rule 4 Medical details**
Do not store medical diagnoses, prescriptions, or treatment history unless a domain schema explicitly supports that consent model.

---

**Rule 5 Legal accusations**
Do not store allegations, criminal claims, or legal judgments as user memory.

---

**Rule 6 Sexual content**
Do not store sexual preferences, explicit sexual content, or intimate details.

---

**Rule 7 Children and minors**
Do not store sensitive information about minors beyond safe educational context.

---

**Rule 8 One-time tasks**
Do not store temporary requests such as "summarize this email" or "translate this paragraph."

---

**Rule 9 Greetings and filler**
Do not store greetings, thanks, small talk, acknowledgements, or conversation filler.

---

**Rule 10 AI-authored claims**
Do not store facts invented by the assistant. Store only user-provided information.

---

**Rule 11 Uncertain guesses**
Do not store inferred facts with low confidence.

---

**Rule 12 Third-party private data**
Do not store private details about other people unless directly relevant and low risk.

---

**Rule 13 Short-lived state**
Do not store temporary mood, current page, current error, or one-off UI state.

---

**Rule 14 Duplicate memory**
Do not re-store a memory already present in existing memory context.

---

**Rule 15 Offensive labels**
Do not store insulting or demeaning labels about the user or other people.

## 5. Confidence Threshold

Only extract memories with confidence greater than or equal to 0.65.

Discard anything below this threshold.
