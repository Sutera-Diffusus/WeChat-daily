import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(scriptDir, "..");
const projectRoot = path.resolve(desktopDir, "..");
const source = path.join(projectRoot, "src", "wechat_bridge", "web", "assets", "editorial", "wei-daily-logo.svg");
const target = path.join(desktopDir, "src", "assets", "wei-daily-logo.svg");

const svg = readFileSync(source, "utf8");
if (!svg.includes("<title id=\"title\">微日报</title>")) {
  throw new Error(`品牌源文件不是预期的微日报 logo：${source}`);
}
mkdirSync(path.dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`brand: synced ${path.relative(projectRoot, source)} -> ${path.relative(projectRoot, target)}`);
