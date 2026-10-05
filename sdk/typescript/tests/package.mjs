// Test the tarball as a customer installation, reusing the source contract fixtures.
// No publication, backend requests, provider calls or additional dependencies.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
assert.equal(lock.version, manifest.version);
assert.equal(lock.packages[""].version, manifest.version);
const npmCli = process.env.npm_execpath;
assert.ok(npmCli, "Run via npm run test:package so the current npm CLI is reused");
const scratch = realpathSync(mkdtempSync(join(tmpdir(), "memoryo-sdk-package-")));

function run(command, args, cwd, capture = false) {
  const result = spawnSync(command, args, {
    cwd, encoding: "utf8", timeout: 120_000,
    stdio: capture ? "pipe" : "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    if (capture) process.stderr.write(`${result.stdout ?? ""}${result.stderr ?? ""}`);
    throw new Error(`${command} exited with ${result.status}`);
  }
  return result.stdout;
}

function replaceFixture(source, from, to) {
  assert.equal(source.split(from).length, 2, `Expected exactly one fixture import: ${from}`);
  return source.replace(from, to);
}

try {
  // Run real prepack, not --ignore-scripts: stale dist must never pass this gate.
  const [packed] = JSON.parse(run(process.execPath,
    [npmCli, "pack", "--json", "--foreground-scripts=false", "--pack-destination", scratch], root, true));
  assert.equal(packed.name, manifest.name);
  assert.equal(packed.version, manifest.version);
  assert.deepEqual(packed.files.map(({ path }) => path).sort(), [
    "LICENSE", "README.md", "dist/index.cjs", "dist/index.d.cts",
    "dist/index.d.ts", "dist/index.js", "package.json",
  ].sort(), "Unexpected package contents (including private/source/build files)");
  assert.equal(packed.filename, `${manifest.name}-${manifest.version}.tgz`);
  const tarball = join(scratch, packed.filename);
  const consumer = join(scratch, "consumer");
  mkdirSync(consumer);
  writeFileSync(join(consumer, "package.json"), JSON.stringify({ private: true, type: "module" }));
  run(process.execPath, [npmCli, "install", tarball, "--offline", "--ignore-scripts",
    "--no-audit", "--no-fund", "--package-lock=false"], consumer);
  const installed = JSON.parse(readFileSync(join(consumer, "node_modules", manifest.name, "package.json"), "utf8"));
  assert.equal(installed.version, manifest.version);
  for (const field of ["dependencies", "optionalDependencies", "peerDependencies"]) {
    assert.deepEqual(installed[field] ?? {}, {}, `Unexpected runtime dependency: ${field}`);
  }

  const contracts = readFileSync(join(root, "tests", "contract.mjs"), "utf8");
  const originalImport = 'import { MemoryOS, MemoryOSError, UniversalMemoryOS } from "../dist/index.js";';
  writeFileSync(join(consumer, "contract-esm.mjs"), replaceFixture(contracts, originalImport,
    `import { MemoryOS, MemoryOSError, UniversalMemoryOS } from "${manifest.name}";`));
  writeFileSync(join(consumer, "contract-cjs.mjs"), replaceFixture(contracts, originalImport,
    `import { createRequire } from "node:module";\nconst { MemoryOS, MemoryOSError, UniversalMemoryOS } = createRequire(import.meta.url)("${manifest.name}");`));
  run(process.execPath, ["--test", "contract-esm.mjs", "contract-cjs.mjs"], consumer);

  // Positive and negative type assertions use the installed public export map.
  const types = replaceFixture(readFileSync(join(root, "tests", "preferences.types.ts"), "utf8"),
    'from "../src/index"', `from "${manifest.name}"`);
  for (const extension of ["mts", "cts"]) {
    writeFileSync(join(consumer, `preferences.${extension}`),
      `import { MemoryOS } from "${manifest.name}";\n${types}\nvoid new MemoryOS("test").get({ query: "q", externalUserId: "u", preferenceContext: read });\n`);
  }
  const tsc = join(root, "node_modules", "typescript", "bin", "tsc");
  const common = { target: "ES2022", strict: true, noEmit: true, skipLibCheck: false,
    lib: ["ES2022", "DOM", "DOM.Iterable"], exactOptionalPropertyTypes: true, noUncheckedIndexedAccess: true };
  for (const resolution of ["Node16", "NodeNext", "Bundler"]) {
    const config = `tsconfig.${resolution}.json`;
    writeFileSync(join(consumer, config), JSON.stringify({
      compilerOptions: { ...common, module: resolution === "Bundler" ? "ESNext" : resolution, moduleResolution: resolution },
      include: resolution === "Bundler" ? ["preferences.mts"] : ["preferences.mts", "preferences.cts"],
    }));
    run(process.execPath, [tsc, "-p", config], consumer);
  }
  console.log(JSON.stringify({ package: packed.id, files: packed.files.length,
    sha256: createHash("sha256").update(readFileSync(tarball)).digest("hex"),
    checks: ["installed ESM/CJS contracts", "Node16/NodeNext ESM/CJS types", "Bundler types", "bounded package inventory"],
    published: false }, null, 2));
} finally {
  // Only remove the exact disposable directory this process created, never a caller path.
  const withinTemp = relative(realpathSync(tmpdir()), scratch);
  assert.ok(withinTemp.startsWith("memoryo-sdk-package-") && !withinTemp.includes(sep));
  rmSync(scratch, { recursive: true, force: true });
}
