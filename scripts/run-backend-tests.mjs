import { delimiter } from "node:path";
import { spawnSync } from "node:child_process";

const python = process.platform === "win32" ? "python" : "python3";
const env = { ...process.env };
env.PYTHONPATH = ["backend", env.PYTHONPATH].filter(Boolean).join(delimiter);
const result = spawnSync(
  python,
  ["-m", "unittest", "discover", "-s", "backend/tests", "-v"],
  { env, stdio: "inherit" },
);
if (result.error) {
  throw result.error;
}
process.exit(result.status ?? 1);
