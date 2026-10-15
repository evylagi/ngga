import express from "express";
import { spawn } from "node:child_process";
import http from "node:http";

const app = express();
const PORT = Number(process.env.PORT || 4096);
const OPENCODE_PORT = Number(process.env.OPENCODE_PORT || 4097);
const OPENCODE_HOST = "127.0.0.1";
const USERNAME = process.env.OPENCODE_SERVER_USERNAME || "opencode";
const PASSWORD = process.env.OPENCODE_SERVER_PASSWORD;

if (!PASSWORD) {
  console.error("FATAL: OPENCODE_SERVER_PASSWORD is not set.");
  process.exit(1);
}

let child = null;
let stopping = false;
let restartTimer = null;
let readinessTimer = null;
let backendReady = false;
let restartCount = 0;

function checkBackend(timeoutMs = 2000) {
  return new Promise((resolve) => {
    const req = http.get({
      hostname: OPENCODE_HOST,
      port: OPENCODE_PORT,
      path: "/",
      timeout: timeoutMs,
      headers: {
        Authorization: "Basic " + Buffer.from(`${USERNAME}:${PASSWORD}`).toString("base64")
      }
    }, (response) => {
      response.resume();
      // Any HTTP response means the backend accepted the connection.
      resolve(response.statusCode >= 200 && response.statusCode < 500);
    });

    req.on("timeout", () => req.destroy(new Error("Backend health check timed out")));
    req.on("error", () => resolve(false));
  });
}

function scheduleRestart(reason) {
  if (stopping || restartTimer) return;

  backendReady = false;
  child = null;
  restartCount += 1;
  const delay = Math.min(30000, 2000 * restartCount);
  console.error(`OpenCode will restart in ${delay}ms. Reason: ${reason}`);

  restartTimer = setTimeout(() => {
    restartTimer = null;
    startOpenCode();
  }, delay);
}

async function monitorBackend() {
  if (readinessTimer) clearInterval(readinessTimer);

  readinessTimer = setInterval(async () => {
    if (stopping || !child) {
      backendReady = false;
      return;
    }

    const ready = await checkBackend();
    if (ready && !backendReady) {
      console.log(`OpenCode backend is ready on 127.0.0.1:${OPENCODE_PORT}`);
      restartCount = 0;
    } else if (!ready && backendReady) {
      console.error("OpenCode is running but no longer responding.");
    }
    backendReady = ready;
  }, 3000);

  readinessTimer.unref();
}

function startOpenCode() {
  if (stopping || child) return;

  backendReady = false;
  console.log(`Starting OpenCode web server on port ${OPENCODE_PORT}...`);

  let proc;
  try {
    proc = spawn("opencode", [
      "web",
      "--hostname", OPENCODE_HOST,
      "--port", String(OPENCODE_PORT)
    ], {
      env: {
        ...process.env,
        OPENCODE_SERVER_PASSWORD: PASSWORD,
        OPENCODE_SERVER_USERNAME: USERNAME
      },
      stdio: "inherit"
    });
  } catch (error) {
    console.error("Could not launch OpenCode:", error.message);
    scheduleRestart(error.message);
    return;
  }

  child = proc;

  proc.once("spawn", () => {
    console.log(`OpenCode process started (pid ${proc.pid}). Waiting for HTTP readiness...`);
  });

  proc.on("error", (error) => {
    console.error("OpenCode process error:", error.message);
    if (child === proc) scheduleRestart(error.message);
  });

  proc.on("exit", (code, signal) => {
    console.error(`OpenCode exited (code=${code}, signal=${signal || "none"}).`);
    if (child === proc) scheduleRestart(`process exited with code ${code}`);
  });

  void monitorBackend();
}

app.get("/health", async (_req, res) => {
  const ready = Boolean(child) && await checkBackend();
  backendReady = ready;
  res.status(ready ? 200 : 503).json({
    status: ready ? "ok" : "unavailable",
    upstream: ready ? "ready" : "not responding",
    upstreamPort: OPENCODE_PORT
  });
});

app.use((req, res) => {
  const expected = "Basic " + Buffer.from(`${USERNAME}:${PASSWORD}`).toString("base64");
  if (req.headers.authorization !== expected) {
    res.set("WWW-Authenticate", 'Basic realm="OpenCode"');
    return res.status(401).send("Authentication required");
  }

  // Check the backend at request time; do not rely on a stale startup flag.
  checkBackend().then((ready) => {
    backendReady = ready;
    if (!ready) {
      console.error("Request not forwarded: OpenCode backend is not responding.");
      return res.status(503).send("OpenCode backend is unavailable. Check Railway deployment logs.");
    }

    const headers = { ...req.headers, host: `${OPENCODE_HOST}:${OPENCODE_PORT}` };
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

    upstream.on("timeout", () => upstream.destroy(new Error("OpenCode request timed out")));
    upstream.on("error", (error) => {
      console.error("OpenCode upstream request failed:", error.message);
      backendReady = false;
      if (!res.headersSent) res.status(502).send("OpenCode backend connection failed.");
      else res.end();
    });

    req.pipe(upstream);
  }).catch((error) => {
    console.error("Backend check failed:", error.message);
    if (!res.headersSent) res.status(503).send("OpenCode backend is unavailable.");
  });
});

const server = app.listen(PORT, "0.0.0.0", () => {
  console.log(`Wrapper listening on 0.0.0.0:${PORT}`);
  startOpenCode();
});

function shutdown() {
  if (stopping) return;
  stopping = true;
  if (restartTimer) clearTimeout(restartTimer);
  if (readinessTimer) clearInterval(readinessTimer);

  server.close();
  if (child) {
    const proc = child;
    child = null;
    proc.kill("SIGTERM");
    setTimeout(() => {
      try { proc.kill("SIGKILL"); } catch {}
    }, 5000).unref();
  }
  setTimeout(() => process.exit(0), 100).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
