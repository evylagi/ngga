const OPENCODE_URL = process.env.OPENCODE_SERVER_URL;
const OPENCODE_USER = process.env.OPENCODE_SERVER_USERNAME || "opencode";
const OPENCODE_PASS = process.env.OPENCODE_SERVER_PASSWORD;

if (!OPENCODE_URL || !OPENCODE_PASS) {
  console.error("Missing required environment variables");
  process.exit(1);
}

const authHeader =
  "Basic " +
  Buffer.from(`${OPENCODE_USER}:${OPENCODE_PASS}`).toString("base64");

async function sendPrompt(prompt) {
  const res = await fetch(`${OPENCODE_URL}/session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: authHeader,
    },
    body: JSON.stringify({ prompt }),
  });

  if (!res.ok) {
    throw new Error(`Server error: ${res.status}`);
  }

  const data = await res.json();

  return (
    data?.response ||
    data?.message ||
    data?.output ||
    JSON.stringify(data)
  );
}

export { sendPrompt };

console.log("OpenCode client started");
