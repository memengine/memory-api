# Extraction Evaluation Report

- Processed examples: 3/3
- Overall accuracy: 100.00%
- All 6 categories seen: False
- Importance scores are spread: True
- Average cost per conversation: $0.000234
- Max cost per conversation: $0.000307
- Retry logic passed: True (calls=3)
- Quota exhausted during run: False

## Checklist

- overall accuracy >= 88%: True
- all 6 categories appear: False
- importance scores are spread: True
- average token cost under $0.002: True
- retry logic 3 attempts then graceful failure: True

## Accuracy Per Category

- expertise: 100.00% (5/5)
- fact: 100.00% (3/3)
- goal: 100.00% (1/1)

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
- if the user corrects or expresses frustration about a response style, score that preference 8 or 9
- if new information contradicts a likely prior memory, add "UPDATES PREVIOUS: [description]" in reasoning
- if the user returns to the same topic multiple times in this conversation, boost the importance_score by 1 point
- when the user says "we use X" rather than "I use X", reduce confidence by 0.1
- if the user describes advanced troubleshooting, migration pain, version-specific nuance, or explains why a technical choice matters, that usually reveals expertise rather than a temporary problem
- current use of a tool, framework, database, or version usually belongs to expertise, not fact
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

- Cost: $0.000307
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
    "content": "User is building a FastAPI backend.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are working on a FastAPI backend, indicating familiarity with this framework."
  },
  {
    "content": "User is using PostgreSQL as their database.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are using PostgreSQL, indicating familiarity with this database."
  },
  {
    "content": "User uses SQLAlchemy for ORM in their backend.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they have been using SQLAlchemy for their ORM. This is a core component of their backend stack."
  },
  {
    "content": "User uses the async version of SQLAlchemy.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states they are using async SQLAlchemy and provides a reason ('learned the hard way that sync calls block the event loop'), indicating deep understanding and practical experience with async patterns in SQLAlchemy."
  },
  {
    "content": "User uses Alembic for database migrations.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are using Alembic for migrations, indicating they are familiar with this tool for managing database schema changes."
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
    "content": "User is building a FastAPI backend.",
    "category": "expertise",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User explicitly states they are working on a FastAPI backend, indicating familiarity with this framework."
  },
  {
    "content": "User uses the async version of SQLAlchemy.",
    "category": "expertise",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states they are using async SQLAlchemy and provides a reason ('learned the hard way that sync calls block the event loop'), indicating deep understanding and practical experience with async patterns in SQLAlchemy."
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
    "prompt_tokens": 1253,
    "completion_tokens": 453,
    "total_tokens": 1706
  }
]
```

### Example 19

- Cost: $0.000168
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
    "content": "User is deliberating between two pricing tiers: ₹999 and ₹1,499 per month.",
    "category": "goal",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "temporary",
    "reasoning": "User is actively deliberating over a business decision (pricing). This is a specific, immediate decision point."
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
    "prompt_tokens": 1219,
    "completion_tokens": 114,
    "total_tokens": 1333
  }
]
```

### Example 20

- Cost: $0.000228
- Matched expected memories: 3/3
- Missing expected memories: 0
- Extra predicted memories: 1

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
    "content": "User's product is live and has launched.",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states their product has launched and gone live, which is a significant change in their business state."
  },
  {
    "content": "User started with 50 beta users.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states the initial number of beta users for their product."
  },
  {
    "content": "User received positive feedback from 3 beta users.",
    "category": "fact",
    "importance_score": 6.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User mentions receiving positive feedback from their beta users."
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
[
  {
    "content": "User's product is live and has launched.",
    "category": "fact",
    "importance_score": 7.0,
    "confidence": 1.0,
    "expiry": "permanent",
    "reasoning": "User states their product has launched and gone live, which is a significant change in their business state."
  }
]
```

**Usage events**

```json
[
  {
    "event": "extraction_usage",
    "user_id": "eval-user-20",
    "chunk_index": 1,
    "prompt_tokens": 1259,
    "completion_tokens": 255,
    "total_tokens": 1514
  }
]
```

