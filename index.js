import express from "express";
import { spawn } from "node:child_process";
import http from "node:http";

const app = express();
const PORT = Number(process.env.PORT || 4096);
const OPENCODE_PORT = Number(process.env.OPENCODE_PORT || 4097);
const OPENCODE_HOST = "127.0.0.1";
const USERNAME = process.env.OPENCODE_SERVER_USERNAME || "opencode";
const PASSWORD = process.env.OPENCODE_SERVER_PASSWORD;
const RESTART_DELAY_MS = 3000;

if (!PASSWORD) {
  console.error("Missing OPENCODE_SERVER_PASSWORD environment variable");
  process.exit(1);
}

let opencode = null;
let shuttingDown = false;
let restarting = false;
let restartTimer = null;
let backendReady = false;

function checkBackend(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const request = http.get(
      { hostname: OPENCODE_HOST, port: OPENCODE_PORT, path: "/global/health", timeout: timeoutMs },
      (response) => {
        response.resume();
        resolve(response.statusCode >= 200 && response.statusCode < 500);
      }
    );
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(false));
  });
}

async function waitForBackend(maxWaitMs = 30000) {
  const deadline = Date.now() + maxWaitMs;
  while (!shuttingDown && Date.now() < deadline) {
    if (await checkBackend()) {
      backendReady = true;
      console.log("OpenCode backend is responding on port " + OPENCODE_PORT);
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  backendReady = false;
  console.error("OpenCode backend did not become ready within " + maxWaitMs + "ms");
  return false;
}

function startOpenCode() {
  if (shuttingDown || opencode) return;

  backendReady = false;
  console.log("Starting OpenCode backend...");
  const child = spawn(
    "opencode",
    ["web", "--hostname", OPENCODE_HOST, "--port", String(OPENCODE_PORT)],
    {
      env: {
        ...process.env,
        OPENCODE_SERVER_PASSWORD: PASSWORD,
        OPENCODE_SERVER_USERNAME: USERNAME
      },
      stdio: "inherit"
    }
  );
  opencode = child;

  child.on("error", (error) => {
    console.error("OpenCode failed to start:", error.message);
  });

  child.on("exit", (code, signal) => {
    console.error(`OpenCode exited (code=${code}, signal=${signal || "none"})`);
    if (opencode === child) opencode = null;
    backendReady = false;
    if (!shuttingDown && !restartTimer) {
      restartTimer = setTimeout(() => {
        restartTimer = null;
        startOpenCode();
      }, RESTART_DELAY_MS);
      restartTimer.unref();
    }
  });

  void waitForBackend();
}

app.get("/health", async (_req, res) => {
  const ready = Boolean(opencode) && await checkBackend();
  backendReady = ready;
  res.status(ready ? 200 : 503).json({
    status: ready ? "ok" : "unavailable",
    upstream: ready ? "ready" : "not responding"
  });
});

app.use((req, res) => {
  const expected = "Basic " + Buffer.from(`${USERNAME}:${PASSWORD}`).toString("base64");
  if (req.headers.authorization !== expected) {
    res.set("WWW-Authenticate", 'Basic realm="OpenCode"');
    return res.status(401).send("Authentication required");
  }

  if (!opencode || !backendReady) {
    return res.status(503).send("OpenCode server is starting or unavailable. Check deployment logs.");
  }

  const headers = { ...req.headers, host: `${OPENCODE_HOST}:${OPENCODE_PORT}` };
  delete headers["content-length"];

  const upstream = http.request({
    hostname: OPENCODE_HOST,
    port: OPENCODE_PORT,
    path: req.originalUrl,
    method: req.method,
    headers,
    timeout: 120000
  }, (response) => {
    res.writeHead(response.statusCode || 502, response.headers);
    response.pipe(res);
  });

  upstream.on("timeout", () => upstream.destroy(new Error("Upstream request timed out")));
  upstream.on("error", (error) => {
    console.error("OpenCode upstream error:", error.message);
    backendReady = false;
    if (!res.headersSent) res.status(502).send("OpenCode server unavailable");
    else res.end();
  });
  req.pipe(upstream);
});

const server = app.listen(PORT, "0.0.0.0", () => {
  console.log(`OpenCode web wrapper listening on port ${PORT}`);
  startOpenCode();
});

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  if (restartTimer) clearTimeout(restartTimer);
  server.close();
  if (opencode) {
    const child = opencode;
    opencode = null;
    child.kill("SIGTERM");
    setTimeout(() => child.kill("SIGKILL"), 5000).unref();
  }
  setTimeout(() => process.exit(0), 100).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
