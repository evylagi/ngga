const OPENCODE_URL = "https://ngga-production.up.railway.app";
const OPENCODE_USER = "opencode";
const OPENCODE_PASS = "keem";

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

async function main() {
  const prompt = process.argv.slice(2).join(" ");

  if (!prompt) {
    console.log('Usage: node index.js "Your prompt here"');
    return;
  }

  try {
    const reply = await sendPrompt(prompt);
    console.log(reply);
  } catch (error) {
    console.error("Request failed:", error.message);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  main();
}

export { sendPrompt };
