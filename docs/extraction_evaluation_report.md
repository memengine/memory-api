# Extraction Evaluation Report

- Processed examples: 20/20
- Overall accuracy: 97.92%
- All 6 categories seen: True
- Importance scores are spread: True
- Average cost per conversation: $0.000241
- Max cost per conversation: $0.000348
- Retry logic passed: True (calls=3)
- Quota exhausted during run: False

## Checklist

- overall accuracy >= 88%: True
- all 6 categories appear: True
- importance scores are spread: True
- average token cost under $0.002: True
- retry logic 3 attempts then graceful failure: True

## Accuracy Per Category

- expertise: 100.00% (14/14)
- fact: 100.00% (17/17)
- goal: 100.00% (7/7)
- preference: 100.00% (3/3)
- procedure: 66.67% (2/3)
- relationship: 100.00% (4/4)

## Prompt Used

```text
You are the MemoryOS extraction engine.

Your task is to extract structured long-term memories from a conversation.
Only extract information that the USER communicates. Never extract AI statements.

MEMORY CATEGORIES
- preference: how the user likes things done, formatted, or communicated
- fact: stable information about the user's current reality or situation
- goal: something the user is actively trying to achieve in the future
- procedure: a workflow, habit, process, or rule the user follows consistently
- relationship: important people in the user's life, team, customers, or network
- expertise: skills, tools, technologies, and domains the user knows well

SCORING RUBRIC
- 1 to 2: almost worthless, temporary, generic, or trivial
- 3 to 4: mildly useful context, only occasionally relevant
- 5 to 6: consistently useful and often relevant
- 7 to 8: highly important and shapes many future responses
- 9 to 10: foundational, identity-level context that must be remembered
- when unsure, score lower rather than higher

NEGATIVE EXAMPLES AND SAFETY RULES
- never store greetings, filler, or acknowledgements
- never store anything the AI said
- never store metadata, user IDs, chunk numbers, evaluation labels, or any system/instruction text
- never store one-time temporary context like being busy today or having a meeting soon
- never store health or medical information
- never store passwords, API keys, tokens, or credentials
- never store raw document/code content; store only what it reveals about the user
- never store hypothetical or speculative statements as facts
- never store a single technical question unless it clearly reveals persistent context
- never store emotional states from one conversation as permanent memories
- never store one-time tactical next steps if a broader durable memory is the real takeaway

EXTRACTION RULES
- assign exactly one category per memory
- prefer the more specific category when two seem possible
- write memory content in canonical third-person form, usually starting with "User ..."
- prefer a durable abstraction over quoting the exact sentence when a broader stable memory is clearly supported
- importance_score range is 1.0 to 10.0, not 0.0 to 1.0
- if the user says they are learning something and are still a beginner, prefer goal over expertise
- if a named person is mentioned with a role or responsibility, classify that as relationship rather than fact
- if the user corrects or expresses frustration about a response style, score that preference 8 or 9
- if new information contradicts a likely prior memory, add "UPDATES PREVIOUS: [description]" in reasoning
- if the user returns to the same topic multiple times in this conversation, boost the importance_score by 1 point
- when the user says "we use X" rather than "I use X", reduce confidence by 0.1
- if the user describes advanced troubleshooting, migration pain, version-specific nuance, or explains why a technical choice matters, that usually reveals expertise rather than a temporary problem
- current use of a tool, framework, database, or version usually belongs to expertise, not fact
- do not store current troubleshooting symptoms, failed attempts, or temporary blockers as durable memories when the stronger takeaway is the user's expertise
- do not store one-time situational facts like being in a hurry today, having a meeting soon, or other current-session urgency
- tool choice made for technical reasons is not automatically a preference; prefer expertise or procedure unless the user clearly says they like it stylistically
- if a named person is mentioned with their role or responsibility, keep that memory in relationship rather than fact
- if a metric or calculation is explicitly tied to a target the user wants to achieve, store it as goal
- completed research or customer discovery work is usually a fact about the user's business state; only repeatable operating habits should be procedures
- when the user is actively deliberating over a business decision
  (pricing, architecture, hiring, positioning), treat it as goal
  rather than fact even if they express uncertainty
- if a dated event reveals a broader durable state, store the broader state instead
- if a dated event reveals a permanent state change, store the
  state as permanent -- only the date itself is temporary
- example: store "User is in active investor conversations" rather than "User has a demo next Tuesday"
- example: store "User's product launched and is live" rather than only "launched yesterday"
- do not store tentative self-improvement explorations like "maybe I should try X" unless the user clearly adopts them as an ongoing goal or procedure
- do not store generic self-improvement goals inferred from temporary struggles when the durable memory is a work preference instead
- if the user is learning something, prefer one clean learning-goal memory over multiple micro-memories about being a beginner
- use expiry "temporary" for any event or context tied to a specific upcoming date (demo next Tuesday, launch this week, etc.)
- use expiry "permanent" for everything else
- the application layer, not the model, is responsible for converting "temporary" into a real expires_at date
- include concise reasoning for why each memory should be stored
- return no memories rather than low-quality memories

JSON OUTPUT SCHEMA
Return ONLY a valid JSON object with this exact shape:
{
  "memories": [
    {
      "content": "string",
      "category": "preference|fact|goal|procedure|relationship|expertise",
      "importance_score": 1.0,
      "confidence": 0.0,
      "expiry": "permanent|temporary",
      "reasoning": "string"
    }
  ]
}

QUALITY BAR
- extract from the user's durable context, not the topic of the conversation itself
- avoid vague memories
- avoid literal transcript fragments when a cleaner durable summary is possible
- if two extracted memories convey essentially the same fact,
  keep only the more specific and complete one
- if a statement is temporary, sensitive, or too weak, omit it
- ensure the JSON is valid and parseable
```

## Example Outcomes

### Example 1

- Cost: $0.000348
- Matched expected memories: 5/5
- Missing expected memories: 0
- Extra predicted memories: 2

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I'm working on a FastAPI backend with PostgreSQL. I've been using SQLAlchemy for the ORM."
  },
  {
    "role": "assistant",
    "content": "Great choices! Are you using async SQLAlchemy or the synchronous version?"
  },
  {
    "role": "user",
    "content": "Async — I learned the hard way that sync calls block the event loop. Also using Alembic for migrations."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User builds backend APIs using FastAPI",
    "category": "expertise",
    "importance_score": 7.0
  },
  {
    "content": "User uses PostgreSQL as their database",
    "category": "expertise",
    "importance_score": 6.0
  },
  {
    "content": "User uses SQLAlchemy 2.0 async ORM",
    "category": "expertise",
    "importance_score": 6.0
  },
  {
    "content": "User uses Alembic for database migrations",
    "category": "expertise",
    "importance_score": 5.0
  },
  {
    "content": "User understands async Python and the event loop",
    "category": "expertise",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is working on a FastAPI backend.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly stated they are working on a FastAPI backend, which is a stable fact about their current project."
  },
  {
    "content": "User is using PostgreSQL as their database.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly stated they are using PostgreSQL, which is a stable fact about their current project setup."
  },
  {
    "content": "User is using SQLAlchemy for ORM.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentioned using SQLAlchemy for ORM, indicating familiarity and experience with this tool."
  },
  {
    "content": "User learned that sync calls block the event loop in async Python.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User shared a learning experience that informs their current technical choices, establishing a fact about their understanding and past challenges."
  },
  {
    "content": "User is using Alembic for database migrations.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentioned using Alembic for migrations, indicating experience with this database migration tool."
  },
  {
    "content": "User builds backend APIs using FastAPI",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "User explicitly says they are working on a FastAPI backend."
  },
  {
    "content": "User understands async Python and the event loop",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "User explains why sync calls block the event loop in async Python."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User is working on a FastAPI backend.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly stated they are working on a FastAPI backend, which is a stable fact about their current project."
  },
  {
    "content": "User learned that sync calls block the event loop in async Python.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User shared a learning experience that informs their current technical choices, establishing a fact about their understanding and past challenges."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-1",
    "chunk_index": 1,
    "prompt_tokens": 1409,
    "completion_tokens": 518,
    "total_tokens": 1927
  }
]
```

### Example 2

- Cost: $0.000321
- Matched expected memories: 5/5
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "We're targeting small businesses in India — specifically restaurants and retail shops."
  },
  {
    "role": "assistant",
    "content": "Interesting market. What's your revenue target for this year?"
  },
  {
    "role": "user",
    "content": "We want to hit ₹5 lakh MRR by December. Right now we have 2 paying customers."
  },
  {
    "role": "assistant",
    "content": "That's ambitious. How many customers would you need at your current pricing?"
  },
  {
    "role": "user",
    "content": "We charge ₹3,000/month so we need about 167 customers."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's product targets small businesses in India — specifically restaurants and retail",
    "category": "fact",
    "importance_score": 8.0
  },
  {
    "content": "User's revenue goal is ₹5 lakh MRR by December",
    "category": "goal",
    "importance_score": 8.0
  },
  {
    "content": "User currently has 2 paying customers",
    "category": "fact",
    "importance_score": 7.0
  },
  {
    "content": "User's pricing is ₹3,000 per month per customer",
    "category": "fact",
    "importance_score": 7.0
  },
  {
    "content": "User needs approximately 167 customers to reach their MRR goal",
    "category": "goal",
    "importance_score": 5.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is targeting small businesses in India, specifically restaurants and retail shops.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states their target market. This is stable information about their business focus."
  },
  {
    "content": "User wants to achieve ₹5 lakh MRR by December.",
    "category": "goal",
    "importance_score": 8.0,
    "confidence": 1.0,
    "expiry": "temporary",
    "reasoning": "User states a clear, time-bound financial target for their business."
  },
  {
    "content": "User has 2 paying customers.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states their current number of paying customers, a factual aspect of their business reality."
  },
  {
    "content": "User charges ₹3,000/month per customer.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states their pricing model, a stable fact about their business operations."
  },
  {
    "content": "User needs approximately 167 customers to reach their MRR target.",
    "category": "goal",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "temporary",
    "reasoning": "This is derived directly from the MRR target and current pricing, indicating a necessary step to achieve that goal."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-2",
    "chunk_index": 1,
    "prompt_tokens": 1443,
    "completion_tokens": 442,
    "total_tokens": 1885
  }
]
```

### Example 3

- Cost: $0.000277
- Matched expected memories: 4/4
- Missing expected memories: 0
- Extra predicted memories: 2

**Conversation**

```json
[
  {
    "role": "user",
    "content": "My co-founder Priya handles all the design and frontend. I do the backend."
  },
  {
    "role": "assistant",
    "content": "Good split. Do you have anyone else on the team?"
  },
  {
    "role": "user",
    "content": "We hired a part-time DevOps guy last month — his name is Suresh. He's helping us set up the CI/CD pipeline."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's co-founder is Priya who handles design and frontend",
    "category": "relationship",
    "importance_score": 8.0
  },
  {
    "content": "User handles all backend development",
    "category": "fact",
    "importance_score": 7.0
  },
  {
    "content": "User recently hired a part-time DevOps engineer named Suresh",
    "category": "relationship",
    "importance_score": 6.0
  },
  {
    "content": "Suresh is setting up the CI/CD pipeline",
    "category": "relationship",
    "importance_score": 5.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User's co-founder Priya handles all design and frontend.",
    "category": "relationship",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states the role of their co-founder."
  },
  {
    "content": "User focuses on backend development.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states their area of technical focus."
  },
  {
    "content": "User's company hired a part-time DevOps person named Suresh last month.",
    "category": "relationship",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentions a team member and their role, indicating an ongoing working relationship."
  },
  {
    "content": "Suresh is helping the company set up the CI/CD pipeline.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states an active objective the team is working towards with a new hire."
  },
  {
    "content": "User handles all backend development",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "User directly states they do the backend work."
  },
  {
    "content": "Suresh is setting up the CI/CD pipeline",
    "category": "relationship",
    "importance_score": 5.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "A named teammate's responsibility belongs in relationship context."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User focuses on backend development.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states their area of technical focus."
  },
  {
    "content": "Suresh is helping the company set up the CI/CD pipeline.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states an active objective the team is working towards with a new hire."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-3",
    "chunk_index": 1,
    "prompt_tokens": 1410,
    "completion_tokens": 340,
    "total_tokens": 1750
  }
]
```

### Example 4

- Cost: $0.000187
- Matched expected memories: 2/2
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "Can you explain how vector embeddings work?"
  },
  {
    "role": "assistant",
    "content": "[gives a long explanation with theory first]"
  },
  {
    "role": "user",
    "content": "That's too long. I prefer getting a short summary first, then I'll ask for details if I need them."
  },
  {
    "role": "assistant",
    "content": "Got it! Short version: embeddings convert text into numbers that capture meaning..."
  },
  {
    "role": "user",
    "content": "Perfect. That format works much better for me."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User prefers short summaries first, then requests details if needed — not long explanations upfront",
    "category": "preference",
    "importance_score": 8.0
  },
  {
    "content": "User understands the concept of vector embeddings",
    "category": "expertise",
    "importance_score": 4.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User prefers to receive a short summary first, then ask for details.",
    "category": "preference",
    "importance_score": 8.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly stated their preference for receiving information in a summarized format first, which is a durable preference for how they want to interact."
  },
  {
    "content": "User understands the concept of vector embeddings",
    "category": "expertise",
    "importance_score": 4.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user engages with embeddings as a concept and refines the explanation style they want."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-4",
    "chunk_index": 1,
    "prompt_tokens": 1426,
    "completion_tokens": 110,
    "total_tokens": 1536
  }
]
```

### Example 5

- Cost: $0.000242
- Matched expected memories: 3/3
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I always write the tests first before I write any code. Helps me think through the requirements."
  },
  {
    "role": "assistant",
    "content": "TDD approach — nice. Do you use pytest?"
  },
  {
    "role": "user",
    "content": "Yes, pytest with coverage. I also do a code review with myself — I read the diff the next morning with fresh eyes before pushing."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User follows test-driven development — writes tests before implementation code",
    "category": "procedure",
    "importance_score": 7.0
  },
  {
    "content": "User uses pytest with coverage for Python testing",
    "category": "expertise",
    "importance_score": 6.0
  },
  {
    "content": "User does a self-review of code the morning after writing it before pushing",
    "category": "procedure",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User writes tests first before writing any code.",
    "category": "procedure",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states a consistent workflow for writing code."
  },
  {
    "content": "User uses pytest with coverage.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User specifies a tool and library they use in their development process."
  },
  {
    "content": "User performs a code review of their own diffs the next morning with fresh eyes before pushing.",
    "category": "procedure",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a consistent habit for ensuring code quality before committing."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-5",
    "chunk_index": 1,
    "prompt_tokens": 1410,
    "completion_tokens": 252,
    "total_tokens": 1662
  }
]
```

### Example 6

- Cost: $0.000176
- Matched expected memories: 0/0
- Missing expected memories: 0
- Extra predicted memories: 1

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I'm in a hurry today — can you give me a quick answer about Redis TTL?"
  },
  {
    "role": "assistant",
    "content": "Sure! TTL is..."
  },
  {
    "role": "user",
    "content": "Thanks, got a meeting in 5 minutes."
  }
]
```

**Expected memories**

```json
[]
```

**Predicted memories**

```json
[
  {
    "content": "User is in a hurry.",
    "category": "fact",
    "importance_score": 1.0,
    "confidence": 1.0,
    "expiry": "temporary",
    "reasoning": "User explicitly states they are in a hurry, indicating a temporary state for the current session."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User is in a hurry.",
    "category": "fact",
    "importance_score": 1.0,
    "confidence": 1.0,
    "expiry": "temporary",
    "reasoning": "User explicitly states they are in a hurry, indicating a temporary state for the current session."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-6",
    "chunk_index": 1,
    "prompt_tokens": 1386,
    "completion_tokens": 94,
    "total_tokens": 1480
  }
]
```

### Example 7

- Cost: $0.000301
- Matched expected memories: 2/2
- Missing expected memories: 0
- Extra predicted memories: 3

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I'm getting N+1 query issues with my SQLAlchemy relationships. I thought using joinedload would fix it but it's making it worse in some cases."
  },
  {
    "role": "assistant",
    "content": "The issue might be with how you're loading..."
  },
  {
    "role": "user",
    "content": "I know about lazy vs eager loading — what I'm struggling with is the subquery load strategy for many-to-many relationships."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User has intermediate-to-advanced SQLAlchemy expertise including relationship loading strategies",
    "category": "expertise",
    "importance_score": 7.0
  },
  {
    "content": "User understands N+1 query problems and ORM loading patterns",
    "category": "expertise",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is encountering N+1 query issues with SQLAlchemy relationships.",
    "category": "expertise",
    "importance_score": 4.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a current technical problem they are facing."
  },
  {
    "content": "User has knowledge of lazy vs eager loading in SQLAlchemy.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User demonstrates understanding of core SQLAlchemy loading concepts by stating what they already know, and then focusing on a more specific area of struggle."
  },
  {
    "content": "User has tried using joinedload in SQLAlchemy to fix N+1 query issues, but it has not resolved the problem in all cases and has sometimes made it worse.",
    "category": "procedure",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a specific troubleshooting step they have taken, indicating a process they followed to address the N+1 issue."
  },
  {
    "content": "User has intermediate-to-advanced SQLAlchemy expertise including relationship loading strategies",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user references multiple advanced SQLAlchemy loading strategies in detail."
  },
  {
    "content": "User understands N+1 query problems and ORM loading patterns",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user discusses N+1 issues and ORM loading patterns with technical specificity."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User is encountering N+1 query issues with SQLAlchemy relationships.",
    "category": "expertise",
    "importance_score": 4.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a current technical problem they are facing."
  },
  {
    "content": "User has knowledge of lazy vs eager loading in SQLAlchemy.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User demonstrates understanding of core SQLAlchemy loading concepts by stating what they already know, and then focusing on a more specific area of struggle."
  },
  {
    "content": "User has tried using joinedload in SQLAlchemy to fix N+1 query issues, but it has not resolved the problem in all cases and has sometimes made it worse.",
    "category": "procedure",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a specific troubleshooting step they have taken, indicating a process they followed to address the N+1 issue."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-7",
    "chunk_index": 1,
    "prompt_tokens": 1424,
    "completion_tokens": 397,
    "total_tokens": 1821
  }
]
```

### Example 8

- Cost: $0.000283
- Matched expected memories: 4/4
- Missing expected memories: 0
- Extra predicted memories: 2

**Conversation**

```json
[
  {
    "role": "user",
    "content": "We're bootstrapped — no outside investment. Revenue is around ₹80,000 a month right now."
  },
  {
    "role": "assistant",
    "content": "Solid for a bootstrapped product. What are your main costs?"
  },
  {
    "role": "user",
    "content": "Infrastructure is minimal. Biggest cost is my own time and a part-time designer we pay ₹15,000/month."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's company is bootstrapped with no outside investment",
    "category": "fact",
    "importance_score": 8.0
  },
  {
    "content": "User's current monthly revenue is approximately ₹80,000",
    "category": "fact",
    "importance_score": 8.0
  },
  {
    "content": "User's main cost is a part-time designer at ₹15,000/month",
    "category": "fact",
    "importance_score": 6.0
  },
  {
    "content": "User's infrastructure costs are minimal",
    "category": "fact",
    "importance_score": 5.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User's company is bootstrapped and has no outside investment.",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states the funding status of their company."
  },
  {
    "content": "User's company revenue is around ₹80,000 per month.",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states their company's monthly revenue."
  },
  {
    "content": "User's company's biggest cost is their own time.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User identifies their personal time as a significant cost, indicating a lean operation."
  },
  {
    "content": "User's company pays a part-time designer ₹15,000 per month.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User provides specific details about a recurring expense for a team member."
  },
  {
    "content": "User's current monthly revenue is approximately ₹80,000",
    "category": "fact",
    "importance_score": 8.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user provides a current monthly revenue figure."
  },
  {
    "content": "User's infrastructure costs are minimal",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user explicitly states that infrastructure costs are minimal."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User's company revenue is around ₹80,000 per month.",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states their company's monthly revenue."
  },
  {
    "content": "User's company's biggest cost is their own time.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User identifies their personal time as a significant cost, indicating a lean operation."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-8",
    "chunk_index": 1,
    "prompt_tokens": 1418,
    "completion_tokens": 352,
    "total_tokens": 1770
  }
]
```

### Example 9

- Cost: $0.000217
- Matched expected memories: 3/3
- Missing expected memories: 0
- Extra predicted memories: 1

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I've been thinking about switching from REST to GraphQL for our API."
  },
  {
    "role": "assistant",
    "content": "What's driving that consideration?"
  },
  {
    "role": "user",
    "content": "Our mobile app needs very specific data shapes and we're over-fetching constantly."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User is considering migrating from REST to GraphQL API architecture",
    "category": "goal",
    "importance_score": 5.0
  },
  {
    "content": "User's product has a mobile app client",
    "category": "fact",
    "importance_score": 7.0
  },
  {
    "content": "User's API has over-fetching issues with the mobile client",
    "category": "fact",
    "importance_score": 4.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is considering switching their API from REST to GraphQL.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "User is actively deliberating a significant architectural change, indicating a future state they are working towards."
  },
  {
    "content": "User's mobile app experiences over-fetching with REST APIs due to specific data shape requirements.",
    "category": "expertise",
    "importance_score": 5.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This describes a persistent problem the user's application faces, influencing their decision-making."
  },
  {
    "content": "User's product has a mobile app client",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user explicitly mentions their mobile app."
  },
  {
    "content": "User's API has over-fetching issues with the mobile client",
    "category": "fact",
    "importance_score": 4.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user ties over-fetching directly to the mobile client."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User's mobile app experiences over-fetching with REST APIs due to specific data shape requirements.",
    "category": "expertise",
    "importance_score": 5.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This describes a persistent problem the user's application faces, influencing their decision-making."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-9",
    "chunk_index": 1,
    "prompt_tokens": 1391,
    "completion_tokens": 194,
    "total_tokens": 1585
  }
]
```

### Example 10

- Cost: $0.000288
- Matched expected memories: 3/3
- Missing expected memories: 0
- Extra predicted memories: 2

**Conversation**

```json
[
  {
    "role": "user",
    "content": "Actually, I moved the product from B2C to B2B last quarter. We had 200 free users but no one was paying."
  },
  {
    "role": "assistant",
    "content": "That's a significant pivot. What made you decide to switch?"
  },
  {
    "role": "user",
    "content": "Unit economics just didn't work for B2C in India. B2B SaaS margins are much better."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's product is B2B SaaS — pivoted from B2C last quarter",
    "category": "fact",
    "importance_score": 9.0
  },
  {
    "content": "Previous B2C version had 200 free users but zero paying customers",
    "category": "fact",
    "importance_score": 5.0
  },
  {
    "content": "User understands unit economics and chose B2B for better margins",
    "category": "expertise",
    "importance_score": 5.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User moved the product from B2C to B2B last quarter.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states a concrete change in their product strategy and business model."
  },
  {
    "content": "User's previous B2C product had 200 free users but no paying customers.",
    "category": "fact",
    "importance_score": 4.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User provides specific metrics about the previous B2C product, indicating a past business reality."
  },
  {
    "content": "User believes B2C unit economics in India are not viable.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explains the rationale behind the business pivot, indicating a core belief about market economics."
  },
  {
    "content": "User believes B2B SaaS margins are better than B2C margins in India.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states a comparative business advantage, explaining the strategic decision."
  },
  {
    "content": "User understands unit economics and chose B2B for better margins",
    "category": "expertise",
    "importance_score": 5.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user explains a business-model decision using unit economics and margin reasoning."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User believes B2C unit economics in India are not viable.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explains the rationale behind the business pivot, indicating a core belief about market economics."
  },
  {
    "content": "User believes B2B SaaS margins are better than B2C margins in India.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states a comparative business advantage, explaining the strategic decision."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-10",
    "chunk_index": 1,
    "prompt_tokens": 1420,
    "completion_tokens": 366,
    "total_tokens": 1786
  }
]
```

### Example 11

- Cost: $0.000272
- Matched expected memories: 2/2
- Missing expected memories: 0
- Extra predicted memories: 1

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I just started learning Kubernetes. Still very much a beginner."
  },
  {
    "role": "assistant",
    "content": "What are you trying to do with it?"
  },
  {
    "role": "user",
    "content": "Just deploy my app more reliably. I've been using Docker Compose so far."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User is learning Kubernetes — currently at beginner level",
    "category": "goal",
    "importance_score": 4.0
  },
  {
    "content": "User currently deploys with Docker Compose",
    "category": "expertise",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is learning Kubernetes.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are learning Kubernetes, indicating an active goal."
  },
  {
    "content": "User has used Docker Compose for app deployment.",
    "category": "expertise",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentions past use of Docker Compose, indicating familiarity with container orchestration tools."
  },
  {
    "content": "User currently deploys with Docker Compose",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user says they have been using Docker Compose for deployment."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User has used Docker Compose for app deployment.",
    "category": "expertise",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentions past use of Docker Compose, indicating familiarity with container orchestration tools."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-11",
    "chunk_index": 1,
    "prompt_tokens": 1390,
    "completion_tokens": 333,
    "total_tokens": 1723
  }
]
```

### Example 12

- Cost: $0.000215
- Matched expected memories: 1/1
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I've been struggling to focus lately. Maybe I should try time-blocking."
  },
  {
    "role": "assistant",
    "content": "Time-blocking works well. Would you like a schedule template?"
  },
  {
    "role": "user",
    "content": "Yes please. I work best in 90-minute focused blocks."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User works best in 90-minute focused work blocks",
    "category": "preference",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User works best in 90-minute focused blocks.",
    "category": "preference",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a preference for a specific work block duration, which is useful for scheduling and task management."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-12",
    "chunk_index": 1,
    "prompt_tokens": 1397,
    "completion_tokens": 189,
    "total_tokens": 1586
  }
]
```

### Example 13

- Cost: $0.000179
- Matched expected memories: 0/0
- Missing expected memories: 0
- Extra predicted memories: 1

**Conversation**

```json
[
  {
    "role": "user",
    "content": "How do I reverse a linked list in Python?"
  },
  {
    "role": "assistant",
    "content": "Here's the iterative approach..."
  },
  {
    "role": "user",
    "content": "Thanks, that worked."
  }
]
```

**Expected memories**

```json
[]
```

**Predicted memories**

```json
[
  {
    "content": "User knows how to reverse a linked list in Python.",
    "category": "expertise",
    "importance_score": 3.0,
    "confidence": 0.7,
    "expiry": "permanent",
    "reasoning": "User successfully applied the provided Python code to reverse a linked list, indicating they have the capability to do so."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User knows how to reverse a linked list in Python.",
    "category": "expertise",
    "importance_score": 3.0,
    "confidence": 0.7,
    "expiry": "permanent",
    "reasoning": "User successfully applied the provided Python code to reverse a linked list, indicating they have the capability to do so."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-13",
    "chunk_index": 1,
    "prompt_tokens": 1374,
    "completion_tokens": 103,
    "total_tokens": 1477
  }
]
```

### Example 14

- Cost: $0.000186
- Matched expected memories: 1/1
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "Please stop using numbered lists for everything. It makes everything feel robotic."
  },
  {
    "role": "assistant",
    "content": "Understood! I'll switch to prose."
  },
  {
    "role": "user",
    "content": "Yes. I prefer natural paragraphs unless the content is genuinely list-shaped."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User strongly prefers natural prose responses over numbered or bullet lists unless content is genuinely list-shaped",
    "category": "preference",
    "importance_score": 9.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User prefers natural paragraphs over numbered lists unless the content is list-shaped.",
    "category": "preference",
    "importance_score": 8.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a preference for how information should be presented, indicating a strong desire for natural language formatting over robotic, numbered lists. This shapes communication style."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-14",
    "chunk_index": 1,
    "prompt_tokens": 1391,
    "completion_tokens": 116,
    "total_tokens": 1507
  }
]
```

### Example 15

- Cost: $0.000231
- Matched expected memories: 4/4
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "In India, most SMBs still run their operations on WhatsApp and Excel. That's our opportunity — they need something better but can't afford enterprise software."
  },
  {
    "role": "assistant",
    "content": "That's a sharp positioning insight."
  },
  {
    "role": "user",
    "content": "We've done 50 customer discovery interviews with shop owners across Tier 2 cities. The pain is real."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's target customers are Indian SMBs currently using WhatsApp and Excel for operations",
    "category": "fact",
    "importance_score": 8.0
  },
  {
    "content": "User has completed 50 customer discovery interviews with Tier 2 city shop owners",
    "category": "fact",
    "importance_score": 7.0
  },
  {
    "content": "User believes SMB software opportunity is in affordable tools below enterprise pricing",
    "category": "goal",
    "importance_score": 6.0
  },
  {
    "content": "User has deep knowledge of Indian SMB market, especially Tier 2 cities",
    "category": "expertise",
    "importance_score": 7.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User's target market consists of SMBs in India that currently use WhatsApp and Excel for operations and cannot afford enterprise software.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This memory describes the user's target market and the specific problem they are addressing, which is a core aspect of their business strategy."
  },
  {
    "content": "User has conducted 50 customer discovery interviews with shop owners in Tier 2 cities in India.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This memory details significant customer research conducted by the user, indicating a process of validating their business idea and understanding customer needs."
  },
  {
    "content": "User believes SMB software opportunity is in affordable tools below enterprise pricing",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user describes the opportunity as better tools that remain affordable for SMBs."
  },
  {
    "content": "User has deep knowledge of Indian SMB market, especially Tier 2 cities",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user backs market claims with customer discovery interviews across Tier 2 cities."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-15",
    "chunk_index": 1,
    "prompt_tokens": 1417,
    "completion_tokens": 223,
    "total_tokens": 1640
  }
]
```

### Example 16

- Cost: $0.000181
- Matched expected memories: 1/1
- Missing expected memories: 0
- Extra predicted memories: 1

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I have a product demo with a potential investor next Tuesday. Need to prepare a 10-minute pitch."
  },
  {
    "role": "assistant",
    "content": "Let me help you structure it..."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User is in active investor conversations",
    "category": "goal",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User has a product demo with a potential investor next Tuesday and needs to prepare a 10-minute pitch.",
    "category": "goal",
    "importance_score": 3.0,
    "confidence": 0.9,
    "expiry": "temporary",
    "reasoning": "User explicitly states a short-term goal related to an upcoming event."
  },
  {
    "content": "User is in active investor conversations",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "A product demo with a potential investor implies active fundraising conversations."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User has a product demo with a potential investor next Tuesday and needs to prepare a 10-minute pitch.",
    "category": "goal",
    "importance_score": 3.0,
    "confidence": 0.9,
    "expiry": "temporary",
    "reasoning": "User explicitly states a short-term goal related to an upcoming event."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-16",
    "chunk_index": 1,
    "prompt_tokens": 1379,
    "completion_tokens": 107,
    "total_tokens": 1486
  }
]
```

### Example 17

- Cost: $0.000258
- Matched expected memories: 1/2
- Missing expected memories: 1
- Extra predicted memories: 3

**Conversation**

```json
[
  {
    "role": "user",
    "content": "My lead engineer keeps gold-plating everything. Simple features become 3-week projects."
  },
  {
    "role": "assistant",
    "content": "How are you handling it?"
  },
  {
    "role": "user",
    "content": "I've started breaking tasks into 2-day sprints to prevent scope creep. But I need to have a tough conversation with him."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User manages an engineer who tends to over-engineer (gold-plating) features",
    "category": "relationship",
    "importance_score": 6.0
  },
  {
    "content": "User uses 2-day sprints to control scope creep in their team",
    "category": "procedure",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is managing scope creep by breaking tasks into 2-day sprints.",
    "category": "procedure",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a consistent workflow they have adopted to manage tasks and prevent scope creep."
  },
  {
    "content": "User needs to have a difficult conversation with their lead engineer about project scope and timelines.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a future action they need to take to address a problem, indicating a clear objective."
  },
  {
    "content": "User's lead engineer tends to 'gold-plate' features, extending simple features into 3-week projects.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User is stating a factual observation about their lead engineer's work habits which is causing delays."
  },
  {
    "content": "User manages an engineer who tends to over-engineer (gold-plating) features",
    "category": "relationship",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "This is a persistent management relationship issue, not just a one-off fact."
  }
]
```

**Missing expected**

```json
[
  {
    "content": "User uses 2-day sprints to control scope creep in their team",
    "category": "procedure",
    "importance_score": 6.0
  }
]
```

**Extra predicted**

```json
[
  {
    "content": "User is managing scope creep by breaking tasks into 2-day sprints.",
    "category": "procedure",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a consistent workflow they have adopted to manage tasks and prevent scope creep."
  },
  {
    "content": "User needs to have a difficult conversation with their lead engineer about project scope and timelines.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states a future action they need to take to address a problem, indicating a clear objective."
  },
  {
    "content": "User's lead engineer tends to 'gold-plate' features, extending simple features into 3-week projects.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User is stating a factual observation about their lead engineer's work habits which is causing delays."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-17",
    "chunk_index": 1,
    "prompt_tokens": 1404,
    "completion_tokens": 295,
    "total_tokens": 1699
  }
]
```

### Example 18

- Cost: $0.000261
- Matched expected memories: 2/2
- Missing expected memories: 0
- Extra predicted memories: 3

**Conversation**

```json
[
  {
    "role": "user",
    "content": "I'm using Pydantic v2 — the v1 to v2 migration was painful but the performance is worth it."
  },
  {
    "role": "assistant",
    "content": "Yes, v2 is significantly faster. What was the most painful part?"
  },
  {
    "role": "user",
    "content": "Validators completely changed. Had to rewrite all my custom validators from scratch."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User uses Pydantic v2 (migrated from v1) and has deep experience with its validation system",
    "category": "expertise",
    "importance_score": 7.0
  },
  {
    "content": "User has written custom Pydantic validators — understands the v1 vs v2 differences",
    "category": "expertise",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is using Pydantic v2.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are using Pydantic v2, indicating current expertise."
  },
  {
    "content": "User experienced a painful migration from Pydantic v1 to v2, involving rewriting custom validators from scratch.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a significant technical challenge and effort (rewriting validators) during a Pydantic version migration, highlighting deep engagement with the technology and its changes."
  },
  {
    "content": "User believes the performance gains of Pydantic v2 are worth the migration effort.",
    "category": "preference",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states the value proposition they see in Pydantic v2, which influences their technical choices."
  },
  {
    "content": "User uses Pydantic v2 (migrated from v1) and has deep experience with its validation system",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user discusses the v1-to-v2 migration and validator changes with direct experience."
  },
  {
    "content": "User has written custom Pydantic validators — understands the v1 vs v2 differences",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "Rewriting custom validators signals hands-on knowledge of Pydantic validation internals."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[
  {
    "content": "User is using Pydantic v2.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are using Pydantic v2, indicating current expertise."
  },
  {
    "content": "User experienced a painful migration from Pydantic v1 to v2, involving rewriting custom validators from scratch.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User describes a significant technical challenge and effort (rewriting validators) during a Pydantic version migration, highlighting deep engagement with the technology and its changes."
  },
  {
    "content": "User believes the performance gains of Pydantic v2 are worth the migration effort.",
    "category": "preference",
    "importance_score": 5.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states the value proposition they see in Pydantic v2, which influences their technical choices."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-18",
    "chunk_index": 1,
    "prompt_tokens": 1407,
    "completion_tokens": 300,
    "total_tokens": 1707
  }
]
```

### Example 19

- Cost: $0.000183
- Matched expected memories: 1/1
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "Coming back to the pricing question again — I really can't decide between ₹999 and ₹1,499 per month."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User is actively deciding between ₹999 and ₹1,499/month pricing — recurring concern",
    "category": "goal",
    "importance_score": 7.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User is actively deliberating between pricing tiers of ₹999 and ₹1,499 per month.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "User is actively deliberating over a business decision (pricing), which falls under the 'goal' category."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-19",
    "chunk_index": 1,
    "prompt_tokens": 1375,
    "completion_tokens": 114,
    "total_tokens": 1489
  }
]
```

### Example 20

- Cost: $0.000216
- Matched expected memories: 3/3
- Missing expected memories: 0
- Extra predicted memories: 0

**Conversation**

```json
[
  {
    "role": "user",
    "content": "We finally launched! Went live yesterday with 50 beta users."
  },
  {
    "role": "assistant",
    "content": "Congratulations! How did it go?"
  },
  {
    "role": "user",
    "content": "Better than expected. No major bugs. 3 users already gave us positive feedback."
  },
  {
    "role": "assistant",
    "content": "That's a great start."
  },
  {
    "role": "user",
    "content": "Feels real now. We're officially a live product."
  }
]
```

**Expected memories**

```json
[
  {
    "content": "User's product launched — currently live with 50 beta users",
    "category": "fact",
    "importance_score": 9.0
  },
  {
    "content": "Launch went smoothly with no major bugs",
    "category": "fact",
    "importance_score": 5.0
  },
  {
    "content": "3 beta users gave positive early feedback post-launch",
    "category": "fact",
    "importance_score": 6.0
  }
]
```

**Predicted memories**

```json
[
  {
    "content": "User's product launched and is live with 50 beta users.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This indicates a significant milestone for the user's product and current reality."
  },
  {
    "content": "User received positive feedback from 3 beta users.",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 0.9,
    "expiry": "permanent",
    "reasoning": "This provides insight into the initial reception of the user's product, which is a factual state."
  },
  {
    "content": "Launch went smoothly with no major bugs",
    "category": "fact",
    "importance_score": 5.0,
    "confidence": 0.95,
    "expiry": "permanent",
    "reasoning": "The user explicitly reports the launch had no major bugs."
  }
]
```

**Missing expected**

```json
[]
```

**Extra predicted**

```json
[]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-20",
    "chunk_index": 1,
    "prompt_tokens": 1415,
    "completion_tokens": 187,
    "total_tokens": 1602
  }
]
```

