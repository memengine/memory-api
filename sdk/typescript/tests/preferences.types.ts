// Compile-only consumer contract checks. Never invokes a service.
import type { MemoryVisibility, PreferenceApplicability, PreferenceClaimRequest, PreferenceReadContext, PreferenceReadDecision, PreferenceScopeRequest, RetrieveResult } from "../src/index";

const scopeQuestion: PreferenceScopeRequest = { state: "application_scope_required",
  propertyKeys: ["examples.language"], availableScopes: [{ kind: "global" }] };
function readScope(result: RetrieveResult): PreferenceScopeRequest | null {
  return result.preferenceScopeRequest ?? null;
}
// @ts-expect-error A scope question cannot resolve a value.
const resolvedScope: PreferenceScopeRequest["state"] = "resolved";
// @ts-expect-error Offered application scopes must be known.
const guessedScope: PreferenceScopeRequest["availableScopes"] = [{ kind: "unresolved" }];

const global: PreferenceApplicability = { kind: "global" };
const visibility: MemoryVisibility = { state: "unavailable", scope: "affected_context" };
function readVisibility(result: RetrieveResult): MemoryVisibility | null {
  return result.memoryVisibility ?? null;
}
// @ts-expect-error Completion is not a withholding state.
const completedVisibility: MemoryVisibility = { state: "completed", scope: "affected_context" };
// @ts-expect-error Unknown scope must not silently authorize context.
const unknownVisibility: MemoryVisibility = { state: "processing", scope: "global" };
const project: PreferenceApplicability = { kind: "context", namespace: "project", key: "release-check" };
const uncertain: PreferenceClaimRequest = {
  propertyKey: "examples.language", value: "Python", applicability: project,
  turnIndex: 0, quote: "Python", commitment: "uncertain",
};
const replacement: PreferenceClaimRequest = {
  ...uncertain, commitment: "replacement", replacesMemoryId: "server-id", replacesVersion: "server-version",
};
const read: PreferenceReadContext = { applicability: global, propertyKeys: ["examples.language"] };
const held: PreferenceClaimRequest = {
  ...uncertain, applicability: global, commitment: "uncertain", choiceState: "current_choice_unresolved",
  replacesMemoryId: "server-id", replacesVersion: "server-version",
};
const decision: PreferenceReadDecision = { propertyKey: "examples.language", applicability: project,
  state: "current_choice_unresolved" };
function readDecisions(result: RetrieveResult): PreferenceReadDecision[] {
  return result.preferenceDecisions ?? [];
}
// @ts-expect-error Withholding requires the exact target version.
const heldWithoutVersion: PreferenceClaimRequest = { ...uncertain, choiceState: "current_choice_unresolved", replacesMemoryId: "m" };
// @ts-expect-error An asserted value cannot declare an unresolved current choice.
const assertedHold: PreferenceClaimRequest = { ...held, commitment: "asserted" };
// @ts-expect-error Withholding cannot guess unresolved applicability.
const unknownHold: PreferenceClaimRequest = { ...held, applicability: { kind: "unresolved" } };
// @ts-expect-error Candidate-only uncertainty cannot target the current winner.
const candidateWithTarget: PreferenceClaimRequest = { ...held, choiceState: "candidate_only" };
// @ts-expect-error Context cannot omit its stable key.
const missingKey: PreferenceApplicability = { kind: "context", namespace: "project" };
// @ts-expect-error Global cannot silently carry project scope.
const scopedGlobal: PreferenceApplicability = { kind: "global", namespace: "project", key: "x" };
// @ts-expect-error A replacement must cite both backend target and version.
const missingVersion: PreferenceClaimRequest = { propertyKey: "x", value: "Python", applicability: global, turnIndex: 0, quote: "Python", commitment: "replacement", replacesMemoryId: "m" };
// @ts-expect-error Reads cannot guess unresolved applicability.
const unresolved: PreferenceReadContext = { applicability: { kind: "unresolved" }, propertyKeys: ["x"] };
void [visibility, readVisibility, completedVisibility, unknownVisibility,
  replacement, read, held, decision, readDecisions, heldWithoutVersion, assertedHold, unknownHold,
  candidateWithTarget, missingKey, scopedGlobal, missingVersion, unresolved];
