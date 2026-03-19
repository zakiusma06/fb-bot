import { Router, type Request, type Response } from "express";
import { fileURLToPath } from "url";
import https from "https";
import crypto from "crypto";
import fs from "fs";
import path from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();

// Shared events file — Python bots read this to keep their cache up to date
const WEBHOOK_EVENTS_FILE = path.resolve(
  __dirname,
  "../../../../telegram-bot/shopify_webhook_events.jsonl",
);

function verifyShopifyHmac(rawBody: string, hmacHeader: string): boolean {
  const secret = process.env.SHOPIFY_API_SECRET || "";
  if (!secret || !hmacHeader) return true; // skip if secret not set
  const computed = crypto
    .createHmac("sha256", secret)
    .update(rawBody, "utf8")
    .digest("base64");
  try {
    return crypto.timingSafeEqual(Buffer.from(computed), Buffer.from(hmacHeader));
  } catch {
    return false;
  }
}

// POST /api/shopify/webhooks — receives products/create, products/update, products/delete
router.post("/shopify/webhooks", (req: Request, res: Response) => {
  const topic      = (req.headers["x-shopify-topic"] as string) || "";
  const hmacHeader = (req.headers["x-shopify-hmac-sha256"] as string) || "";
  const rawBody    = JSON.stringify(req.body);   // body already parsed by app.ts express.json()

  // HMAC verification (best-effort)
  if (!verifyShopifyHmac(rawBody, hmacHeader)) {
    console.warn("[shopify_webhook] HMAC mismatch — rejecting");
    return res.status(401).send("Unauthorized");
  }

  const product   = req.body as Record<string, unknown>;
  const productId = product["id"] as number | undefined;

  if (!productId) {
    return res.status(400).send("Missing product id");
  }

  let event = "update";
  if      (topic === "products/create") event = "create";
  else if (topic === "products/delete") event = "delete";
  else if (topic === "products/update") event = "update";

  const line = JSON.stringify({
    event,
    product_id:   productId,
    product_data: product,
    ts:           Date.now(),
  });

  try {
    fs.appendFileSync(WEBHOOK_EVENTS_FILE, line + "\n", "utf8");
    console.log(`[shopify_webhook] ${event}/${productId} written to events file`);
  } catch (err) {
    console.error("[shopify_webhook] Failed to write events file:", err);
  }

  return res.status(200).send("OK");
});

router.get("/shopify/callback", async (req, res) => {
  const { code, shop, state } = req.query as Record<string, string>;

  const clientId     = process.env.SHOPIFY_API_KEY     || "";
  const clientSecret = process.env.SHOPIFY_API_SECRET  || "";

  if (!code || !shop) {
    return res.status(400).send(`
      <h2>Missing code or shop parameter</h2>
      <p>code: ${code}</p>
      <p>shop: ${shop}</p>
    `);
  }

  try {
    const body = JSON.stringify({ client_id: clientId, client_secret: clientSecret, code });

    const token: string = await new Promise((resolve, reject) => {
      const options = {
        hostname: shop,
        path: "/admin/oauth/access_token",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      };
      const req2 = https.request(options, (r) => {
        let data = "";
        r.on("data", (chunk) => (data += chunk));
        r.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            if (parsed.access_token) resolve(parsed.access_token);
            else reject(new Error(data));
          } catch (e) {
            reject(new Error(data));
          }
        });
      });
      req2.on("error", reject);
      req2.write(body);
      req2.end();
    });

    return res.send(`
      <!DOCTYPE html>
      <html>
      <head><title>Shopify Token</title>
      <style>
        body { font-family: monospace; padding: 40px; background: #111; color: #0f0; }
        h2 { color: #fff; }
        .token { background: #222; padding: 20px; border-radius: 8px; word-break: break-all; font-size: 18px; }
        .instruction { color: #aaa; margin-top: 20px; }
      </style>
      </head>
      <body>
        <h2>✅ Shopify Access Token Generated</h2>
        <div class="token">${token}</div>
        <p class="instruction">
          Copy the token above and save it as the <strong>SHOPIFY_ACCESS_TOKEN</strong> secret in Replit.
        </p>
      </body>
      </html>
    `);
  } catch (err: any) {
    return res.status(500).send(`<h2>Error exchanging code</h2><pre>${err.message}</pre>`);
  }
});

export default router;
