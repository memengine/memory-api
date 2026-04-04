# MemoryOS — Memory Extraction Specification
**Version:** 1.0  
**Last updated:** 2026  
**Purpose:** Ground truth for the AI extraction engine. Claude Code reads this file before building ExtractionService. Every extraction decision is based on this document.

---

## Table of Contents
1. [Memory Categories — Definitions and Examples](#1-memory-categories)
2. [Importance Scoring Rubric](#2-importance-scoring-rubric)
3. [20 Example Conversations with Expected Extractions](#3-example-conversations)
4. [What Should NEVER Be Stored — 20 Rules](#4-never-store-these)
5. [Edge Cases and Judgment Calls](#5-edge-cases)

---

## 1. Memory Categories

Every extracted memory must belong to exactly ONE of these six categories. If a memory fits two categories, choose the more specific one.

---

### PREFERENCE
**Definition:** A stated or implied personal choice about HOW the user likes things done, presented, or communicated. Preferences are about style, format, and personal taste — not facts about the world.

**Key question to ask:** "Does this tell me how this person LIKES things?"

**Examples:**
- `"User prefers concise bullet-point responses over long paragraphs"`
- `"User likes to receive code examples before explanations, not after"`
- `"User prefers dark mode tools and minimal UI designs"`

**NOT a preference:**
- "User uses Python" → this is EXPERTISE
- "User wants to launch in 3 months" → this is GOAL

---

### FACT
**Definition:** A verifiable, stable piece of information about the user's life, situation, or context. Facts describe who the user IS and what their current situation looks like.

**Key question to ask:** "Does this tell me something TRUE about this person's current reality?"

**Examples:**
- `"User is based in Assam, India"`
- `"User has a team of 3 engineers"`
- `"User's product is currently in private beta"`

**NOT a fact:**
- "User wants to expand to 10 engineers" → this is GOAL
- "User always tests before committing" → this is PROCEDURE

---

### GOAL
**Definition:** Something the user is actively working toward, wants to achieve, or aspires to in the future. Goals are forward-looking — they describe desired future states.

**Key question to ask:** "Does this tell me what this person is TRYING TO ACHIEVE?"

**Examples:**
- `"User wants to reach ₹10 lakh MRR within 12 months"`
- `"User is trying to get accepted into a Y Combinator batch"`
- `"User wants to replace their staffing agency with an AI system"`

**NOT a goal:**
- "User is building a SaaS product" → this is FACT (it's happening now)
- "User prefers to work on goals in the morning" → this is PREFERENCE

---

### PROCEDURE
**Definition:** A specific way the user does something — a workflow, process, habit, or rule they follow consistently. Procedures describe HOW the user operates.

**Key question to ask:** "Does this tell me how this person DOES THINGS in their workflow?"

**Examples:**
- `"User always writes tests before committing code to the repository"`
- `"User reviews all pull requests on Friday afternoons before the weekend"`
- `"User sends weekly update emails to the team every Monday morning"`

**NOT a procedure:**
- "User wants to improve their code review process" → this is GOAL
- "User likes pair programming" → this is PREFERENCE

---

### RELATIONSHIP
**Definition:** Information about people in the user's life — teammates, clients, investors, partners, family. Includes how the user relates to them and their roles.

**Key question to ask:** "Does this tell me about someone IMPORTANT in this person's world?"

**Examples:**
- `"User's technical co-founder is named Rahul and handles all backend work"`
- `"User's main investor is from Blume Ventures and checks in monthly"`
- `"User's first paying customer is a logistics company in Pune"`

**NOT a relationship:**
- "User has 3 engineers" → this is FACT (count, not person-specific)
- "User wants to hire a designer" → this is GOAL

---

### EXPERTISE
**Definition:** Skills, knowledge domains, tools, and technologies the user knows and uses. Expertise describes what the user is CAPABLE of and what they work with.

**Key question to ask:** "Does this tell me what this person KNOWS or is SKILLED IN?"

**Examples:**
- `"User is proficient in Python, FastAPI, and SQLAlchemy"`
- `"User has deep experience in B2B SaaS sales cycles"`
- `"User understands vector databases and embedding-based search well"`

**NOT expertise:**
- "User is learning Rust" → debatable, but GOAL is more appropriate (aspiring, not current)
- "User prefers Python over Go" → this is PREFERENCE

---

## 2. Importance Scoring Rubric

Every memory gets a score from **1 to 10**. This score affects:
- Which memories are retrieved first (higher = retrieved first)
- Which memories decay and get archived (below 3 after 30 days of no access)
- How much weight a memory gets in hybrid retrieval scoring

Use the rubric below. When in doubt, lean toward the LOWER score — it is better to under-score than to pollute retrieval with noise.

---

### Score 1 — Almost worthless, will decay quickly
Information that is either too generic, too temporary, or too trivial to be useful in future conversations.

**Examples at score 1:**
- `"User mentioned it was raining today"` — temporary, irrelevant
- `"User said they like coffee"` — too generic, not actionable
- `"User mentioned they read an article about React"` — passing reference, no depth

**Rule:** If forgetting this memory would not affect the quality of any future AI response, it scores 1.

---

### Score 3 — Mildly useful, provides some context
Information that adds texture but is not critical for the AI to do its job well. Nice to know, but rarely retrieved.

**Examples at score 3:**
- `"User works in the mornings and takes long breaks in the afternoon"`
- `"User prefers Slack over email for team communication"`
- `"User's company was founded in 2024"`

**Rule:** If this memory would occasionally be useful (less than 20% of conversations), it scores 3.

---

### Score 5 — Consistently useful, should be retrieved often
Information that is directly relevant to the user's primary context and would improve the AI's responses in most conversations.

**Examples at score 5:**
- `"User is building a SaaS product targeting Indian SMBs"`
- `"User's team uses Python and FastAPI for the backend"`
- `"User has raised pre-seed funding and is looking for product-market fit"`

**Rule:** If this memory is relevant to roughly half of all conversations with this user, it scores 5.

---

### Score 7 — Highly important, shapes almost every response
Core identity-level information about the user's situation, goals, or expertise. Missing this would cause clearly wrong AI responses.

**Examples at score 7:**
- `"User is the founder and CEO of MemoryOS, an AI infrastructure startup"`
- `"User's primary technical stack is Python, FastAPI, PostgreSQL, and Qdrant"`
- `"User's immediate priority is launching MVP and getting first 10 paying customers"`

**Rule:** If the AI would give a noticeably worse response without this memory, it scores 7.

---

### Score 9 — Critical, foundational, must always be retrieved
The most important facts about who this user is and what they are doing. These memories define the entire context of the relationship.

**Examples at score 9:**
- `"User's name is Aditya and he is the founder of MemoryOS"`
- `"User is building an AI memory infrastructure product — this is his primary project"`
- `"User has no credit card currently and is using only free API tiers for development"`

**Rule:** If forgetting this memory would make the AI seem like it doesn't know the user at all, it scores 9.

---

### Quick Scoring Guide

| Score | Frequency of usefulness | What it affects |
|-------|------------------------|-----------------|
| 1–2   | Rarely, < 10% of conversations | Will decay in 30 days |
| 3–4   | Occasionally, 10–30% | Low retrieval priority |
| 5–6   | Often, 30–60% | Normal retrieval |
| 7–8   | Almost always, 60–80% | High retrieval priority |
| 9–10  | Always, > 80% | Always retrieved first |

---

## 3. Example Conversations

These 20 examples are the **ground truth test set**. The extraction engine must produce output matching these examples with at least 88% accuracy. Each example shows:
- The raw conversation
- The memories that SHOULD be extracted
- The memories that should NOT be extracted (and why)

---

### Example 1 — Technical stack introduction

**Conversation:**
```
User: I'm working on a FastAPI backend with PostgreSQL. I've been using SQLAlchemy for the ORM.
AI: Great choices! Are you using async SQLAlchemy or the synchronous version?
User: Async — I learned the hard way that sync calls block the event loop. Also using Alembic for migrations.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User builds backend APIs using FastAPI" | expertise | 7 | Core technical identity |
| "User uses PostgreSQL as their database" | expertise | 6 | Specific tool choice |
| "User uses SQLAlchemy 2.0 async ORM" | expertise | 6 | Specific version matters |
| "User uses Alembic for database migrations" | expertise | 5 | Part of their toolchain |
| "User understands async Python and the event loop" | expertise | 6 | Depth of knowledge |

**Should NOT extract:**
- "User learned the hard way" — vague, not actionable
- "AI asked about async vs sync" — AI's question, not user information

---

### Example 2 — Business goals

**Conversation:**
```
User: We're targeting small businesses in India — specifically restaurants and retail shops.
AI: Interesting market. What's your revenue target for this year?
User: We want to hit ₹5 lakh MRR by December. Right now we have 2 paying customers.
AI: That's ambitious. How many customers would you need at your current pricing?
User: We charge ₹3,000/month so we need about 167 customers.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's product targets small businesses in India — specifically restaurants and retail" | fact | 8 | Core business context |
| "User's revenue goal is ₹5 lakh MRR by December" | goal | 8 | Specific, time-bound target |
| "User currently has 2 paying customers" | fact | 7 | Current business state |
| "User's pricing is ₹3,000 per month per customer" | fact | 7 | Pricing model |
| "User needs approximately 167 customers to reach their MRR goal" | goal | 5 | Derived metric, useful for context |

**Should NOT extract:**
- "₹5 lakh divided by ₹3,000 equals 167" — arithmetic, not a memory
- AI's calculation confirmation — AI statement, not user information

---

### Example 3 — Team and relationships

**Conversation:**
```
User: My co-founder Priya handles all the design and frontend. I do the backend.
AI: Good split. Do you have anyone else on the team?
User: We hired a part-time DevOps guy last month — his name is Suresh. He's helping us set up the CI/CD pipeline.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's co-founder is Priya who handles design and frontend" | relationship | 8 | Key person, named |
| "User handles all backend development" | fact | 7 | User's primary role |
| "User recently hired a part-time DevOps engineer named Suresh" | relationship | 6 | New team member |
| "Suresh is setting up the CI/CD pipeline" | relationship | 5 | Suresh's specific responsibility |

**Should NOT extract:**
- "User asked if they have more team members" — AI's question
- "Good split" — AI's comment, not information about user

---

### Example 4 — Preferences stated explicitly

**Conversation:**
```
User: Can you explain how vector embeddings work?
AI: [gives a long explanation with theory first]
User: That's too long. I prefer getting a short summary first, then I'll ask for details if I need them.
AI: Got it! Short version: embeddings convert text into numbers that capture meaning...
User: Perfect. That format works much better for me.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User prefers short summaries first, then requests details if needed — not long explanations upfront" | preference | 8 | Directly stated, strong signal |
| "User understands the concept of vector embeddings" | expertise | 4 | Implied by the conversation topic |

**Should NOT extract:**
- "User asked about vector embeddings" — the topic of one conversation, not a persistent fact
- "AI gave a long explanation" — AI behaviour, not user information
- "User said 'that format works much better'" — confirmation of preference already captured

---

### Example 5 — Working procedures

**Conversation:**
```
User: I always write the tests first before I write any code. Helps me think through the requirements.
AI: TDD approach — nice. Do you use pytest?
User: Yes, pytest with coverage. I also do a code review with myself — I read the diff the next morning with fresh eyes before pushing.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User follows test-driven development — writes tests before implementation code" | procedure | 7 | Consistent workflow |
| "User uses pytest with coverage for Python testing" | expertise | 6 | Specific tool + practice |
| "User does a self-review of code the morning after writing it before pushing" | procedure | 6 | Specific habit, valuable context |

**Should NOT extract:**
- "AI asked about pytest" — AI question
- "TDD helps think through requirements" — general statement, not personal fact

---

### Example 6 — One-time context (should NOT be stored)

**Conversation:**
```
User: I'm in a hurry today — can you give me a quick answer about Redis TTL?
AI: Sure! TTL is...
User: Thanks, got a meeting in 5 minutes.
```

**SHOULD extract:**
- NOTHING from this conversation. All context is temporary.

**Should NOT extract:**
- "User was in a hurry" — one-time situational state
- "User had a meeting in 5 minutes" — one-time event
- "User asked about Redis TTL" — one question does not establish expertise

---

### Example 7 — Expertise revealed through problem description

**Conversation:**
```
User: I'm getting N+1 query issues with my SQLAlchemy relationships. I thought using joinedload would fix it but it's making it worse in some cases.
AI: The issue might be with how you're loading...
User: I know about lazy vs eager loading — what I'm struggling with is the subquery load strategy for many-to-many relationships.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User has intermediate-to-advanced SQLAlchemy expertise including relationship loading strategies" | expertise | 7 | Level of knowledge revealed |
| "User understands N+1 query problems and ORM loading patterns" | expertise | 6 | Specific knowledge domain |

**Should NOT extract:**
- "User had N+1 query issues" — temporary problem, not persistent fact
- "User asked about joinedload" — troubleshooting session topic

---

### Example 8 — Financial situation

**Conversation:**
```
User: We're bootstrapped — no outside investment. Revenue is around ₹80,000 a month right now.
AI: Solid for a bootstrapped product. What are your main costs?
User: Infrastructure is minimal. Biggest cost is my own time and a part-time designer we pay ₹15,000/month.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's company is bootstrapped with no outside investment" | fact | 8 | Fundamental business context |
| "User's current monthly revenue is approximately ₹80,000" | fact | 8 | Current financial state |
| "User's main cost is a part-time designer at ₹15,000/month" | fact | 6 | Cost structure |
| "User's infrastructure costs are minimal" | fact | 5 | Useful business context |

**Should NOT extract:**
- "AI said solid for a bootstrapped product" — AI commentary
- Exact calculation of profit margin — derived, not stated

---

### Example 9 — Ambiguous statement that needs category judgment

**Conversation:**
```
User: I've been thinking about switching from REST to GraphQL for our API.
AI: What's driving that consideration?
User: Our mobile app needs very specific data shapes and we're over-fetching constantly.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User is considering migrating from REST to GraphQL API architecture" | goal | 5 | Considering = goal, not fact |
| "User's product has a mobile app client" | fact | 7 | Revealed product detail |
| "User's API has over-fetching issues with the mobile client" | fact | 4 | Current technical problem |

**Should NOT extract:**
- "User uses REST currently" — NOT confirmed explicitly (implied but uncertain)
- Store this as GOAL not FACT because "thinking about" means not decided yet

---

### Example 10 — User corrects previous information

**Conversation:**
```
User: Actually, I moved the product from B2C to B2B last quarter. We had 200 free users but no one was paying.
AI: That's a significant pivot. What made you decide to switch?
User: Unit economics just didn't work for B2C in India. B2B SaaS margins are much better.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's product is B2B SaaS — pivoted from B2C last quarter" | fact | 9 | Core business model, pivoted |
| "Previous B2C version had 200 free users but zero paying customers" | fact | 5 | Useful historical context |
| "User understands unit economics and chose B2B for better margins" | expertise | 5 | Business judgment and knowledge |

**Conflict resolution needed:**
- If a previous memory says "User's product is B2C" — this conversation updates it to B2B
- Old memory should be ARCHIVED with note "Updated: user pivoted to B2B Q1 2026"

---

### Example 11 — Learning something new (low score)

**Conversation:**
```
User: I just started learning Kubernetes. Still very much a beginner.
AI: What are you trying to do with it?
User: Just deploy my app more reliably. I've been using Docker Compose so far.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User is learning Kubernetes — currently at beginner level" | goal | 4 | Aspiring skill, not current expertise |
| "User currently deploys with Docker Compose" | expertise | 6 | Current deployment knowledge |

**Should NOT extract:**
- "User wants to deploy more reliably" — too vague, doesn't add value

**Note on scoring:** Learning something scores lower than knowing it. Beginner Kubernetes = 4, not 7.

---

### Example 12 — Health or personal sensitive information

**Conversation:**
```
User: I've been struggling to focus lately. Maybe I should try time-blocking.
AI: Time-blocking works well. Would you like a schedule template?
User: Yes please. I work best in 90-minute focused blocks.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User works best in 90-minute focused work blocks" | preference | 6 | Specific, actionable work preference |

**Should NOT extract:**
- "User has been struggling to focus" — sensitive personal struggle, temporary state
- "User might have focus issues" — do NOT infer medical or psychological conditions ever
- "User tried time-blocking" — method exploration, not established procedure

---

### Example 13 — Asking for help without revealing information

**Conversation:**
```
User: How do I reverse a linked list in Python?
AI: Here's the iterative approach...
User: Thanks, that worked.
```

**SHOULD extract:**
- NOTHING. One technical question does not establish expertise. The user might be a student or a senior engineer — we cannot tell.

**Rule:** Single technical questions without context about why or the user's level should not generate memories.

---

### Example 14 — Strong preference revealed through frustration

**Conversation:**
```
User: Please stop using numbered lists for everything. It makes everything feel robotic.
AI: Understood! I'll switch to prose.
User: Yes. I prefer natural paragraphs unless the content is genuinely list-shaped.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User strongly prefers natural prose responses over numbered or bullet lists unless content is genuinely list-shaped" | preference | 9 | Very strong signal — expressed frustration |

**Note:** Frustration or correction is a strong signal. Score the resulting preference higher (8–9) than a casually stated preference (5–6).

---

### Example 15 — Industry knowledge and context

**Conversation:**
```
User: In India, most SMBs still run their operations on WhatsApp and Excel. That's our opportunity — they need something better but can't afford enterprise software.
AI: That's a sharp positioning insight.
User: We've done 50 customer discovery interviews with shop owners across Tier 2 cities. The pain is real.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's target customers are Indian SMBs currently using WhatsApp and Excel for operations" | fact | 8 | Specific customer profile |
| "User has completed 50 customer discovery interviews with Tier 2 city shop owners" | fact | 7 | Market research completed |
| "User believes SMB software opportunity is in affordable tools below enterprise pricing" | goal | 6 | Strategic positioning insight |
| "User has deep knowledge of Indian SMB market, especially Tier 2 cities" | expertise | 7 | Research-backed domain knowledge |

---

### Example 16 — Time-sensitive information that should expire

**Conversation:**
```
User: I have a product demo with a potential investor next Tuesday. Need to prepare a 10-minute pitch.
AI: Let me help you structure it...
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User is in active investor conversations" | goal | 6 | Ongoing fundraising activity |

**Should NOT extract as permanent:**
- "User has a demo next Tuesday" — set expiry: 7 days. After that, this is irrelevant.

**Expiry rule:** Any memory with a specific near-future date should have `expires_at` set to that date plus 3 days.

---

### Example 17 — User's communication about others (relationship intelligence)

**Conversation:**
```
User: My lead engineer keeps gold-plating everything. Simple features become 3-week projects.
AI: How are you handling it?
User: I've started breaking tasks into 2-day sprints to prevent scope creep. But I need to have a tough conversation with him.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User manages an engineer who tends to over-engineer (gold-plating) features" | relationship | 6 | Management context |
| "User uses 2-day sprints to control scope creep in their team" | procedure | 6 | Active management technique |

**Should NOT extract:**
- Any negative characterization of the engineer by name — too sensitive
- "User needs to have a tough conversation" — temporary pending action

---

### Example 18 — Expertise with specific version or nuance

**Conversation:**
```
User: I'm using Pydantic v2 — the v1 to v2 migration was painful but the performance is worth it.
AI: Yes, v2 is significantly faster. What was the most painful part?
User: Validators completely changed. Had to rewrite all my custom validators from scratch.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User uses Pydantic v2 (migrated from v1) and has deep experience with its validation system" | expertise | 7 | Specific version + migration experience |
| "User has written custom Pydantic validators — understands the v1 vs v2 differences" | expertise | 6 | Nuanced technical knowledge |

**Should NOT extract:**
- "Migration was painful" — emotional description, not factual
- "Performance is worth it" — opinion, not lasting information

---

### Example 19 — Repeated topic signals importance

**Conversation (third time user mentions this):**
```
User: Coming back to the pricing question again — I really can't decide between ₹999 and ₹1,499 per month.
```

**SHOULD extract / UPDATE:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User is actively deciding between ₹999 and ₹1,499/month pricing — recurring concern" | goal | 7 | Mentioned multiple times = higher importance |

**Rule:** If the extraction engine can identify that this topic has appeared before (via conflict resolution check), boost the importance score by 1–2 points. Recurring topics are more important than single mentions.

---

### Example 20 — Closure and resolution

**Conversation:**
```
User: We finally launched! Went live yesterday with 50 beta users.
AI: Congratulations! How did it go?
User: Better than expected. No major bugs. 3 users already gave us positive feedback.
AI: That's a great start.
User: Feels real now. We're officially a live product.
```

**SHOULD extract:**
| Memory | Category | Score | Reasoning |
|--------|----------|-------|-----------|
| "User's product launched — currently live with 50 beta users" | fact | 9 | Major status change — product is now live |
| "Launch went smoothly with no major bugs" | fact | 5 | Useful historical context |
| "3 beta users gave positive early feedback post-launch" | fact | 6 | Early validation signal |

**Conflict resolution:**
- Previous memory "User's product is in development" → UPDATE to "User's product is live"
- Previous memory "User is preparing for launch" → ARCHIVE

---

## 4. What Should NEVER Be Stored — 20 Rules

These are absolute rules. Even if the information seems useful, do NOT store it if it matches any of these patterns.

---

**Rule 1 — Greetings and openers**
Never store: "Hi", "Hello", "Good morning", "How are you?", "Thanks for your help", "See you later", "Goodbye". These carry zero information.

---

**Rule 2 — AI's own statements**
Never store anything the AI said. Only store what the USER communicates. If the AI says "That's a great idea!", do not store "User has great ideas."

---

**Rule 3 — Filler and acknowledgment phrases**
Never store: "Okay", "Got it", "Sure", "Makes sense", "I see", "Right", "Exactly", "Hmm", "Yeah". These confirm the conversation is happening but contain no information.

---

**Rule 4 — One-time situational context**
Never store: "I'm busy today", "I have a call in 10 minutes", "I'm at the office", "It's raining here". These are temporary states irrelevant to future conversations.

---

**Rule 5 — Health and medical information**
Never store: Any mention of illness, medication, mental health, physical condition, or medical history. This data is too sensitive and poses legal and ethical risks.

---

**Rule 6 — Financial account details**
Never store: Bank account numbers, credit card details, specific account balances, investment positions, or any individually identifying financial data. Revenue, pricing, and funding stage are fine — specific account data is not.

---

**Rule 7 — Passwords and credentials**
Never store: Any password, API key, secret token, auth code, or security credential the user mentions — even accidentally. Immediately discard.

---

**Rule 8 — Speculation and hypotheticals**
Never store: "What if I...", "Imagine if...", "Let's say hypothetically...", "In theory...". Hypotheticals are not facts about the user.

---

**Rule 9 — Questions the user asks**
Never store the question itself as a memory. "User asked how to reverse a linked list" is not a useful memory. Extract only what the question REVEALS about the user's situation if anything.

---

**Rule 10 — AI-confirmed information without user origination**
Never store something as fact just because the AI confirmed it. If AI says "So you're using Python?" and user says "Yes", you can store Python — but only because the user confirmed it, not because the AI suggested it.

---

**Rule 11 — Negative emotional states**
Never store: "User seemed frustrated", "User was confused", "User appeared stressed". Emotional states in one conversation do not define the user permanently.

---

**Rule 12 — Content of documents or code the user shares**
Never store the actual content of code, documents, or data the user pastes. Store only what it REVEALS about the user. "User is working on a Python script that parses CSV files" — not the actual code.

---

**Rule 13 — Other people's private information**
Never store: Details about third parties that the user shares — client data, employee personal information, competitor internal details. Store only what is needed for the user's context.

---

**Rule 14 — Location beyond general region**
Never store: Specific street addresses, home location, or precise location details. Storing "User is based in Assam, India" is fine. Storing "User lives at [specific address]" is not.

---

**Rule 15 — Opinions about other people**
Never store: Negative characterizations of named individuals. "User's co-founder Priya is excellent at design" is fine. "User thinks their investor is difficult to deal with" — do not store.

---

**Rule 16 — Single-use instructions**
Never store: Instructions the user gives for just that conversation. "For this conversation, respond only in Hindi" — do not make this a permanent preference. Only store if the user generalizes it ("I always prefer Hindi").

---

**Rule 17 — Information over 3 years old unless explicitly relevant**
Never store historical facts that are no longer relevant. "User worked at TCS 5 years ago" — low value unless user says it's still relevant to their current work.

---

**Rule 18 — Contradicted information without resolution**
Never store new information that directly contradicts an existing memory without resolving the conflict. Run conflict resolution first, then store the winner.

---

**Rule 19 — Vague quantities without context**
Never store: "User has some experience", "User knows a bit about marketing", "User is fairly technical". Vague qualifiers make the memory useless. Only store if there is enough context to know what "some" means.

---

**Rule 20 — User's emotional reaction to AI responses**
Never store: "User liked this response", "User found the explanation helpful", "User asked for more". These are feedback signals for the session, not permanent memories about the user.

---

## 5. Edge Cases and Judgment Calls

These are situations where the extraction rule is not obvious. Use these as tiebreakers.

---

### When a user says "I usually..." vs "I always..."
- "I usually..." → score 5–6, store as preference
- "I always..." → score 7–8, store as procedure
- "I sometimes..." → score 3–4 at most, or skip

---

### When the same fact appears in multiple ways
Example: User says "We're a small team" in one message and "There are 4 of us" in another.
- Store the SPECIFIC version: "User's team has 4 people"
- Archive the vague version: "User has a small team"

---

### When information is implied but not stated
Example: User asks a very advanced Kubernetes question without stating their level.
- Do NOT infer expertise unless the question strongly implies it
- Rule: require at least 2–3 signals before inferring a skill level

---

### When the user is speaking about their business vs themselves
Example: "We use PostgreSQL" vs "I use PostgreSQL"
- Both are valid — store as the user's context regardless of "I" or "we"
- Note: "we" language is slightly weaker signal (could be team decision, not personal expertise)

---

### When information changes rapidly (startup metrics)
Example: Revenue, customer count, team size
- Always update to the most recent value
- Keep previous value visible in audit log for 90 days
- Score the CURRENT value — old metrics decay faster

---

### When the user is clearly joking
Example: "I'm basically a Kubernetes expert now after 2 days 😄"
- Emoji and self-deprecating humor = low confidence
- Score maximum 3, add note: "user self-reported with humour — verify"
- Better to miss a memory than to store incorrect expertise level

---



