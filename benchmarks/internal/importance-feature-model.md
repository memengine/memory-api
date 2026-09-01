# Deterministic importance feature model

This benchmark-only scorer uses no provider and no benchmark case IDs. It reads the normalized proposition plus attributed supporting turns. For multi-proposition turns, deterministic lexical overlap selects the clause(s) most relevant to the memory before feature extraction. Without attribution it uses supplied turns; ambiguous signals stay neutral.

| Feature | Deterministic derivation | Ordinal meaning |
|---|---|---|
| Temporal scope | Explicit duration, expiry, recurrence, permanence | -2 ephemeral, -1 bounded, 0 unknown, +1 recurring/durable |
| Expertise maturity | General skill-state and experience phrases for expertise | -2 novice/stale, 0 working/unknown, +2 established/senior |
| Goal commitment | Conditional/speculative, active ownership, registration/training for goals | -2 speculative, 0 planned/unknown, +1 active, +2 committed |
| Procedure durability/consequence | Workaround, seasonal, routine, critical-control signals for procedures | -2 workaround, -1 seasonal, 0 routine/unknown, +2 critical |
| Identity breadth | Temporary role, stable identity/relationship/location, foundational identity | -1 temporary, 0 unknown, +1 stable, +2 foundational |
| Preference scope | Project-only versus an unqualified durable preference declaration | -2 scoped, 0 unknown, +2 durable/universal |
| Consequence of forgetting | Explicit safety, production, legal, medical, ownership, low-consequence signals | -1 low, 0 unknown, +1 high |
| Category | Extracted category | Weak prior: 0 to +0.5 |

Formula: add the fixed weighted contributions to 5, apply transparent cross-feature adjustments, then use decimal half-up rounding and clamp to 1–9. Stable fact identity receives a +1 anchor unless positive temporal recurrence already contributes. Response-shaping expertise consequences, and fact consequences without an identity anchor, receive +1 unless recurrence already contributes. Overlapping committed-goal and high-consequence signals receive a -0.5 double-counting correction. Ephemeral scope (`-2`) dominates at score 1 for preferences and score 3 otherwise. A recurring low-consequence procedure with durability `-1` remains at score 3. Pending disposition caps the result at 6. Temporal otherwise weighs .75; identity and consequence .5; domain features 1.0. No feature uses model inference.
