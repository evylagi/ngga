import express from "express";
import { spawn } from "node:child_process";

const app = express();
const PORT = Number(process.env.PORT || 4096);
const PASSWORD = process.env.OPENCODE_SERVER_PASSWORD;

if (!PASSWORD) {
  console.error("Missing OPENCODE_SERVER_PASSWORD environment variable");
  process.exit(1);
}

app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

const opencode = spawn(
  "opencode",
  ["web", "--hostname", "127.0.0.1", "--port", "4097"],
  {
    env: {
      ...process.env,
      OPENCODE_SERVER_PASSWORD: PASSWORD,
      OPENCODE_SERVER_USERNAME:
        process.env.OPENCODE_SERVER_USERNAME || "opencode"
    },
    stdio: "inherit"
  }
);

opencode.on("error", (error) => {
  console.error("OpenCode failed to start:", error);
});

app.use((req, res) => {
  const headers = { ...req.headers, host: "127.0.0.1:4097" };
  delete headers["content-length"];

  const authorization = req.headers.authorization;
  const expected = "Basic " + Buffer.from(
    `${process.env.OPENCODE_SERVER_USERNAME || "opencode"}:${PASSWORD}`
  ).toString("base64");

  if (authorization !== expected) {
    res.set("WWW-Authenticate", 'Basic realm="OpenCode"');
    return res.status(401).send("Authentication required");
  }

  import("http").then(({ request }) => {
    const upstream = request({
      hostname: "127.0.0.1",
      port: 4097,
      path: req.originalUrl,
      method: req.method,
      headers
    }, (response) => {
      res.writeHead(response.statusCode || 502, response.headers);
      response.pipe(res);
    });

    upstream.on("error", (error) => {
      console.error("OpenCode upstream error:", error.message);
      if (!res.headersSent) res.status(502).send("OpenCode server unavailable");
      else res.end();
    });

    req.pipe(upstream);
  });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`OpenCode web wrapper listening on port ${PORT}`);
});

process.on("SIGTERM", () => {
  opencode.kill("SIGTERM");
  process.exit(0);
});
