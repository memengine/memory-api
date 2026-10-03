// Compile-only consumer contract checks. Never invokes a service.
import type { PreferenceApplicability, PreferenceClaimRequest, PreferenceReadContext } from "../src/index";

const global: PreferenceApplicability = { kind: "global" };
const project: PreferenceApplicability = { kind: "context", namespace: "project", key: "release-check" };
const uncertain: PreferenceClaimRequest = {
  propertyKey: "examples.language", value: "Python", applicability: project,
  turnIndex: 0, quote: "Python", commitment: "uncertain",
};
const replacement: PreferenceClaimRequest = {
  ...uncertain, commitment: "replacement", replacesMemoryId: "server-id", replacesVersion: "server-version",
};
const read: PreferenceReadContext = { applicability: global, propertyKeys: ["examples.language"] };
// @ts-expect-error Context cannot omit its stable key.
const missingKey: PreferenceApplicability = { kind: "context", namespace: "project" };
// @ts-expect-error Global cannot silently carry project scope.
const scopedGlobal: PreferenceApplicability = { kind: "global", namespace: "project", key: "x" };
// @ts-expect-error A replacement must cite both backend target and version.
const missingVersion: PreferenceClaimRequest = { propertyKey: "x", value: "Python", applicability: global, turnIndex: 0, quote: "Python", commitment: "replacement", replacesMemoryId: "m" };
// @ts-expect-error Reads cannot guess unresolved applicability.
const unresolved: PreferenceReadContext = { applicability: { kind: "unresolved" }, propertyKeys: ["x"] };
void [replacement, read, missingKey, scopedGlobal, missingVersion, unresolved];
