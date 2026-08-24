import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const node = process.execPath;

execFileSync(node, [path.join(scriptDir, "sync-brand.mjs")], { stdio: "inherit" });
execFileSync(node, [path.join(scriptDir, "generate-icons.mjs")], { stdio: "inherit" });
