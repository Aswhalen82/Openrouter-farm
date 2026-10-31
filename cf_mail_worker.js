/**
 * cf_mail_worker.js — Cloudflare Email Worker that turns a catch-all domain into an
 * unlimited inbox API for the OpenRouter farm.
 *
 * Setup (free tier is enough):
 *   1. Add your domain to Cloudflare, then Email -> Email Routing -> enable.
 *   2. Create a KV namespace:  wrangler kv namespace create MAIL
 *   3. wrangler.toml:
 *        name = "mail-catcher"
 *        main = "cf_mail_worker.js"
 *        compatibility_date = "2026-01-01"
 *        kv_namespaces = [{ binding = "MAIL", id = "<id from step 2>" }]
 *        [vars]
 *        SECRET = "change-me-long-random"
 *   4. wrangler deploy  ->  https://mail-catcher.<you>.workers.dev
 *   5. Email Routing -> Routes -> catch-all -> send to Worker "mail-catcher"
 *
 * Any address @yourdomain lands here. The worker keeps the last 20 mails per address
 * for 30 minutes (KV minimum TTL) and exposes:
 *      GET /code?to=<address>&key=<SECRET>   -> {"codes":["123456"], "count":1}
 *      GET /raw?to=<address>&key=<SECRET>    -> full messages (debug)
 *
 * The farm only needs /code: it creates `x<tag>@yourdomain`, waits for the 6-digit code,
 * and never talks to a third-party mail provider.
 */

const TTL = 1800;              // KV minimum TTL is 60s; 30 min covers slow delivery
const KEEP = 20;               // messages kept per address

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function extractCodes(text) {
  const clean = text
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/#[0-9a-f]{3,8}\b/gi, " ");
  const out = [];
  for (const m of clean.matchAll(/(?<!\d)(\d{6})(?!\d)/g)) {
    if (new Set(m[1]).size > 1 && !out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

async function readMessage(message) {
  const decoder = new TextDecoder();
  const reader = message.raw.getReader();
  let raw = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    raw += decoder.decode(value, { stream: true });
  }
  const headerEnd = raw.indexOf("\r\n\r\n");
  const head = headerEnd > 0 ? raw.slice(0, headerEnd) : "";
  let body = headerEnd > 0 ? raw.slice(headerEnd + 4) : raw;

  // multipart: keep every part, the code is in whichever part is text
  const subjectMatch = head.match(/^subject:\s*(.*)$/im);
  const subject = subjectMatch ? subjectMatch[1].replace(/\r/g, "").trim() : "";
  const decoded = body.replace(/=\r?\n/g, "").replace(/=([0-9A-F]{2})/g, (_, h) =>
    String.fromCharCode(parseInt(h, 16)));

  return { subject, text: decoded.slice(0, 200000) };
}

export default {
  async email(message, env) {
    const to = (message.to || "").toLowerCase();
    if (!to) return;
    let subject = "", text = "";
    try {
      ({ subject, text } = await readMessage(message));
    } catch (e) {
      text = "";
    }
    const blob = `${subject}\n${text}`;
    const codes = extractCodes(blob);

    const key = `box:${to}`;
    const prev = (await env.MAIL.get(key, "json")) || { messages: [] };
    prev.messages.unshift({
      at: Date.now(),
      from: message.from,
      subject,
      codes,
      snippet: text.replace(/\s+/g, " ").slice(0, 400),
    });
    prev.messages = prev.messages.slice(0, KEEP);
    await env.MAIL.put(key, JSON.stringify(prev), { expirationTtl: TTL });
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.searchParams.get("key") !== env.SECRET) return json({ error: "forbidden" }, 403);
    const to = (url.searchParams.get("to") || "").toLowerCase();
    if (!to) return json({ error: "to required" }, 400);

    const box = (await env.MAIL.get(`box:${to}`, "json")) || { messages: [] };
    if (url.pathname === "/raw") return json(box);
    const codes = [];
    for (const m of box.messages) for (const c of m.codes) if (!codes.includes(c)) codes.push(c);
    return json({ codes, count: box.messages.length, message: box.messages[0] || null });
  },
};
