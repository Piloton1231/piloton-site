import { chmodSync, copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const outputName = process.platform === "win32" ? "node-runtime.exe" : "node-runtime";
const destination = fileURLToPath(new URL(`../${outputName}`, import.meta.url));

copyFileSync(process.execPath, destination);
chmodSync(destination, 0o755);
console.log("Bundled Node.js runtime for YouTube signature processing.");
