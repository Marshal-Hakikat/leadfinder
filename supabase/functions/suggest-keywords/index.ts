// Lead Finder: AI keyword helper (fast version, a few seconds).
//
// The website calls this function when someone clicks "Suggest keywords with AI".
// It checks who is asking, saves the request (max 10 a day per person, enforced by
// the database), asks Groq for suggestions, cleans them and returns them.
// If Groq does not answer, the request stays "pending" and the free GitHub job
// (suggest.yml, every 10 minutes) answers it instead.
//
// Secrets (Supabase > Edge Functions > Secrets):
//   GROQ_API_KEY   required   your Groq key
//   GROQ_MODEL     optional   default: openai/gpt-oss-120b
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are provided by Supabase automatically.

import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function reply(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });
}

// ---------- cleaning (same rules as suggest_worker.py) ----------
const SUB_RE = /^[A-Za-z0-9_]{2,30}$/;
const SITE_RE = /^[a-z0-9][a-z0-9.-]{1,40}$/;
const SE_NAMES: Record<string, string> = {
  "stack overflow": "stackoverflow", "stackoverflow": "stackoverflow", "salesforce": "salesforce",
  "super user": "superuser", "server fault": "serverfault", "webmasters": "webmasters", "startups": "startups",
};
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function clean(items: unknown, limit: number, maxLen = 50, words = 4): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of Array.isArray(items) ? items : []) {
    if (typeof raw !== "string") continue;
    const x = raw.trim().replace(/^["'#.,;:]+|["'#.,;:]+$/g, "").replace(/\s+/g, " ").trim();
    if (!x || x.length > maxLen || x.split(" ").length > words) continue;
    const k = x.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(x);
    if (out.length >= limit) break;
  }
  return out;
}

function cleanResult(data: Record<string, unknown>, context: string) {
  const ctx = (context || "").trim().toLowerCase();
  let kws = clean(data.keywords, 20).map((k) => k.toLowerCase());
  if (ctx) kws = kws.map((k) => k.replace(new RegExp(`\\b${esc(ctx)}\\b`, "g"), "").replace(/\s+/g, " ").trim() || k);
  const subs: string[] = [];
  for (const s of Array.isArray(data.subreddits) ? data.subreddits : []) {
    if (typeof s !== "string") continue;
    const n = s.trim().replace(/^\/?r\//i, "");
    if (SUB_RE.test(n) && !subs.some((x) => x.toLowerCase() === n.toLowerCase())) subs.push(n);
  }
  const sites: string[] = [];
  for (const s of Array.isArray(data.stackexchange_sites) ? data.stackexchange_sites : []) {
    if (typeof s !== "string") continue;
    let n = s.trim().toLowerCase().replace(".stackexchange.com", "").replace(".com", "");
    n = SE_NAMES[n] || n;
    if (SITE_RE.test(n) && !sites.includes(n)) sites.push(n);
  }
  return {
    keywords: clean(kws, 12),
    competitors: clean(data.competitors, 10, 40),
    subreddits: subs.slice(0, 8),
    se_sites: sites.slice(0, 4),
    exclude_words: clean(data.avoid_words, 10, 30, 2).map((w) => w.toLowerCase()),
  };
}

function promptFor(name: string, description: string, context: string) {
  return `You help a small software company find people online who need its product.
Product name: ${name}
What it does: ${description}
Topic word (optional): ${context || "-"}

Suggest search words for finding forum posts by people who have the problem this product solves.
Rules:
- keywords: 8 to 12 short phrases (1 to 4 words) that a person with the problem would actually write,
  for example "duplicate leads" or "merge contacts". Lower case. Do not repeat the topic word in every phrase.
- competitors: up to 8 real products that solve the same problem. Only names you are sure exist.
- subreddits: up to 6 real Reddit communities where these people ask questions (name only, no r/).
- stackexchange_sites: up to 3 Stack Exchange sites (for example "salesforce" or "stackoverflow"), or none.
- avoid_words: up to 6 words that mark posts that are NOT buyers (for example "exam", "homework", "hiring").
Reply with ONLY JSON:
{"keywords": [], "competitors": [], "subreddits": [], "stackexchange_sites": [], "avoid_words": []}`;
}

async function askGroq(prompt: string): Promise<Record<string, unknown> | null> {
  const key = Deno.env.get("GROQ_API_KEY");
  if (!key) { console.error("GROQ_API_KEY is not set"); return null; }
  try {
    const r = await fetch(Deno.env.get("GROQ_URL") || "https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: Deno.env.get("GROQ_MODEL") || "openai/gpt-oss-120b",
        messages: [{ role: "user", content: prompt }],
        temperature: 0.3,
        max_completion_tokens: 1500,
        response_format: { type: "json_object" },
      }),
      signal: AbortSignal.timeout(25000),
    });
    if (!r.ok) { console.error("Groq HTTP", r.status, (await r.text()).slice(0, 300)); return null; }
    const text: string = (await r.json())?.choices?.[0]?.message?.content || "";
    const m = text.match(/\{[\s\S]*\}/);
    return m ? JSON.parse(m[0]) : null;
  } catch (e) {
    console.error("Groq failed:", e);
    return null;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return reply(405, { error: "Use POST." });

  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    { auth: { persistSession: false } });

  // 1. Who is asking? (must be a signed-in user)
  const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const { data: who } = await admin.auth.getUser(token);
  if (!who?.user) return reply(401, { error: "Please sign in again." });

  // 2. Read and check the input
  let body: Record<string, unknown> = {};
  try { body = await req.json(); } catch { /* empty body */ }
  const name = String(body.name || "").trim().slice(0, 80);
  const description = String(body.description || "").trim().slice(0, 600);
  const context = String(body.context || "").trim().slice(0, 60);
  if (!name) return reply(400, { error: "Type the product name first." });
  if (description.length < 20) return reply(400, { error: "Describe the problem first (at least 20 characters)." });

  // 3. Save the request (the database allows 10 a day per person)
  const ins = await admin.from("keyword_suggestions")
    .insert({ owner_id: who.user.id, name, description, context }).select("id").single();
  if (ins.error) {
    const limit = /10 suggestions a day/.test(ins.error.message);
    return reply(limit ? 429 : 500, { error: limit ? ins.error.message : "Could not save the request. Please try again." });
  }
  const id = ins.data.id;

  // 4. Ask the AI; if it does not answer, the GitHub job will (status stays "pending")
  const data = await askGroq(promptFor(name, description, context));
  const result = data ? cleanResult(data, context) : null;
  if (!result || !result.keywords.length) return reply(202, { id, pending: true });

  await admin.from("keyword_suggestions")
    .update({ status: "done", result, done_at: new Date().toISOString() }).eq("id", id);
  return reply(200, { id, result });
});
