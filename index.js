import TelegramBot from "node-telegram-bot-api";

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID = process.env.TELEGRAM_ALLOWED_USER_ID;
const OPENCODE_URL = process.env.OPENCODE_SERVER_URL;
const OPENCODE_USER = process.env.OPENCODE_SERVER_USERNAME || "opencode";
const OPENCODE_PASS = process.env.OPENCODE_SERVER_PASSWORD;

if (!BOT_TOKEN || !OPENCODE_URL || !OPENCODE_PASS) {
  console.error("Missing required env vars");
  process.exit(1);
}

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Basic auth header for your existing Railway server
const authHeader =
  "Basic " + Buffer.from(`${OPENCODE_USER}:${OPENCODE_PASS}`).toString("base64");

bot.on("message", async (msg) => {
  const chatId = msg.chat.id;
  const userId = String(msg.from.id);
  const text = msg.text;

  // Auth gate
  if (ALLOWED_USER_ID && userId !== String(ALLOWED_USER_ID)) {
    return bot.sendMessage(chatId, "Unauthorized.");
  }

  if (!text) return;

  if (text === "/status") {
    return bot.sendMessage(chatId, "Bridge is alive. Server: " + OPENCODE_URL);
  }

  try {
    await bot.sendChatAction(chatId, "typing");

    const res = await fetch(`${OPENCODE_URL}/session`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: authHeader,
      },
      body: JSON.stringify({ prompt: text }),
    });

    if (!res.ok) {
      return bot.sendMessage(chatId, `Server error: ${res.status}`);
    }

    const data = await res.json();
    const reply =
      data?.response || data?.message || data?.output || JSON.stringify(data);

    // Telegram has a 4096 char limit
    for (let i = 0; i < reply.length; i += 4000) {
      await bot.sendMessage(chatId, reply.slice(i, i + 4000));
    }
  } catch (err) {
    bot.sendMessage(chatId, `Error: ${err.message}`);
  }
});

console.log("Telegram bridge started");
