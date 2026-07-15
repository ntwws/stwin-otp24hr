const encoder = new TextEncoder();

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
}
function b64(bytes) { return btoa(String.fromCharCode(...bytes)); }
function bytes(size = 32) { const out = new Uint8Array(size); crypto.getRandomValues(out); return out; }
async function sha256(value) { return b64(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)))); }
async function passwordHash(password, salt) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt: encoder.encode(salt), iterations: 100000, hash: "SHA-256" }, key, 256);
  return b64(new Uint8Array(bits));
}
async function phoneHash(env, phone) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(env.PHONE_HASH_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64(new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(String(phone).replace(/\D/g, "")))));
}
function maskPhone(phone) { const p = String(phone).replace(/\D/g, ""); return p.length < 4 ? "****" : `***${p.slice(-4)}`; }

export class PurchaseQueue {
  constructor(ctx) { this.ctx = ctx; }
  async fetch(request) {
    const body = await request.json();
    const now = Date.now();
    let lock = await this.ctx.storage.get("lock");
    if (lock && lock.expiresAt <= now) { await this.ctx.storage.delete("lock"); lock = null; }
    if (body.action === "status") return json({ lock });
    if (body.action === "acquire") {
      if (lock && lock.userId !== body.userId) return json({ error: `${lock.username} กำลังซื้ออยู่ กรุณารอคิว`, acquired: false, lock }, 409);
      lock = { userId: body.userId, username: body.username, expiresAt: now + Math.min(300000, Math.max(60000, body.ttlMs || 180000)) };
      await this.ctx.storage.put("lock", lock); return json({ acquired: true, lock });
    }
    if (body.action === "release") {
      if (!lock || lock.userId === body.userId) await this.ctx.storage.delete("lock");
      return json({ released: true });
    }
    return json({ error: "bad_action" }, 400);
  }
}

async function authenticate(request, env) {
  const raw = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!raw) return null;
  const tokenHash = await sha256(raw);
  return env.DB.prepare(`SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>datetime('now') AND u.active=1`).bind(tokenHash).first();
}
async function queue(env, payload) {
  const id = env.PURCHASE_QUEUE.idFromName("shared-wallet");
  return env.PURCHASE_QUEUE.get(id).fetch("https://queue/", { method: "POST", body: JSON.stringify(payload) });
}
async function heroRequest(env, action, params = {}) {
  const query = new URLSearchParams({ api_key: env.HERO_API_KEY, action });
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  const response = await fetch(`https://hero-sms.com/stubs/handler_api.php?${query}`, {
    headers: { "user-agent": "HeroLineTH-Cloudflare/2.0" }
  });
  const raw = await response.text();
  if (!response.ok) return json({ error: raw || `HeroSMS HTTP ${response.status}` }, 502);
  return json({ raw });
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url), path = url.pathname;
      if (request.method === "GET" && path === "/health") return json({ ok: true });
      const body = request.method === "POST" ? await request.json() : {};

      if (request.method === "POST" && path === "/admin/users") {
        const adminUser = await authenticate(request, env);
        const hasSecret = request.headers.get("x-admin-secret") === env.ADMIN_SECRET;
        if (!hasSecret && adminUser?.role !== "admin") return json({ error: "unauthorized" }, 401);
        const username = String(body.username || "").trim(); const password = String(body.password || "");
        if (!/^[A-Za-z0-9_.-]{3,32}$/.test(username) || password.length < 8) return json({ error: "invalid_username_or_password" }, 400);
        const existing = await env.DB.prepare("SELECT id FROM users WHERE username=? COLLATE NOCASE").bind(username).first();
        if (existing) return json({ error: "Username นี้มีอยู่แล้ว กรุณาใช้ชื่ออื่น" }, 409);
        const salt = b64(bytes(16)), hash = await passwordHash(password, salt);
        await env.DB.prepare("INSERT INTO users(username,password_hash,password_salt,role) VALUES(?,?,?,?)")
          .bind(username, hash, salt, body.role === "admin" ? "admin" : "user").run();
        return json({ ok: true, username });
      }
      if (request.method === "POST" && path === "/auth/login") {
        const user = await env.DB.prepare("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1").bind(String(body.username || "").trim()).first();
        if (!user || await passwordHash(String(body.password || ""), user.password_salt) !== user.password_hash) return json({ error: "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" }, 401);
        const token = b64(bytes(32)), tokenHash = await sha256(token);
        await env.DB.prepare("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,datetime('now','+7 days'))").bind(tokenHash, user.id).run();
        return json({ token, username: user.username, role: user.role });
      }
      const user = await authenticate(request, env);
      if (!user) return json({ error: "กรุณาเข้าสู่ระบบใหม่" }, 401);

      if (path === "/queue/status") return queue(env, { action: "status" });
      if (request.method === "POST" && path === "/queue/acquire") return queue(env, { action: "acquire", userId: user.id, username: user.username, ttlMs: 180000 });
      if (request.method === "POST" && path === "/queue/release") return queue(env, { action: "release", userId: user.id });
      if (request.method === "POST" && path === "/hero/request") {
        const action = String(body.action || "");
        const allowed = new Set(["getBalance", "getPricesExtended", "getFreePrices", "getPricesV2",
          "getPrices", "getNumber", "getStatus", "setStatus"]);
        if (!allowed.has(action)) return json({ error: "ไม่อนุญาตคำสั่ง HeroSMS นี้" }, 400);
        if (action === "getNumber") {
          const statusResponse = await queue(env, { action: "status" });
          const status = await statusResponse.json();
          if (!status.lock || status.lock.userId !== user.id) return json({ error: "กรุณาจองคิวซื้อก่อน" }, 409);
        }
        return heroRequest(env, action, body.params || {});
      }

      if (request.method === "GET" && path === "/me/stats") {
        const row = await env.DB.prepare(`SELECT
          COUNT(CASE WHEN purchased_at>=datetime('now','start of month') THEN 1 END) monthly_purchased,
          COUNT(CASE WHEN otp_received_at IS NOT NULL AND strftime('%Y-%m',otp_received_at)=strftime('%Y-%m','now') THEN 1 END) monthly_success
          FROM activations WHERE user_id=?`).bind(user.id).first();
        return json({ username: user.username, monthly_purchased: row?.monthly_purchased || 0, monthly_success: row?.monthly_success || 0 });
      }
      if (request.method === "POST" && path === "/auth/change-password") {
        const current = String(body.current_password || ""), next = String(body.new_password || "");
        if (next.length < 8) return json({ error: "รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร" }, 400);
        const record = await env.DB.prepare("SELECT password_hash,password_salt FROM users WHERE id=?").bind(user.id).first();
        if (!record || await passwordHash(current, record.password_salt) !== record.password_hash) return json({ error: "รหัสผ่านปัจจุบันไม่ถูกต้อง" }, 400);
        const salt = b64(bytes(16)), hash = await passwordHash(next, salt);
        await env.DB.prepare("UPDATE users SET password_hash=?,password_salt=? WHERE id=?").bind(hash, salt, user.id).run();
        return json({ ok: true });
      }
      if (request.method === "GET" && path === "/admin/stats") {
        if (user.role !== "admin") return json({ error: "ไม่มีสิทธิ์ดูรายงาน" }, 403);
        const rows = await env.DB.prepare(`
          SELECT u.username,
            COUNT(CASE WHEN a.purchased_at >= datetime('now','start of month') THEN 1 END) monthly_purchased,
            COUNT(CASE WHEN a.otp_received_at IS NOT NULL AND strftime('%Y-%m',a.otp_received_at)=strftime('%Y-%m','now') THEN 1 END) monthly_success,
            MAX(a.otp_received_at) last_success
          FROM users u LEFT JOIN activations a ON a.user_id=u.id
          WHERE u.active=1 GROUP BY u.id,u.username ORDER BY monthly_success DESC,u.username
        `).all();
        return json({ month: new Date().toISOString().slice(0, 7), users: rows.results || [] });
      }
      if (request.method === "GET" && path === "/activations/history") {
        const requestedLimit = Number(url.searchParams.get("limit") || 200);
        const requestedOffset = Number(url.searchParams.get("offset") || 0);
        const limit = Number.isFinite(requestedLimit) ? Math.min(500, Math.max(1, Math.trunc(requestedLimit))) : 200;
        const offset = Number.isFinite(requestedOffset) ? Math.max(0, Math.trunc(requestedOffset)) : 0;
        const rows = await env.DB.prepare(`
          SELECT a.activation_id,
            COALESCE(NULLIF(a.phone_number,''),a.phone_mask) phone,
            a.price,a.purchased_at,a.otp_received_at,a.otp_code,a.status,u.username
          FROM activations a JOIN users u ON u.id=a.user_id
          ORDER BY a.purchased_at DESC,a.activation_id DESC
          LIMIT ? OFFSET ?
        `).bind(limit, offset).all();
        const items = rows.results || [];
        return json({ items, has_more: items.length === limit, next_offset: offset + items.length });
      }
      if (request.method === "POST" && path === "/activations/register") {
        const ph = await phoneHash(env, body.phone), mask = maskPhone(body.phone);
        const history = await env.DB.prepare(`SELECT COUNT(*) count, MAX(otp_received_at) last_otp FROM activations WHERE phone_hash=? AND otp_received_at>=datetime('now','-7 days')`).bind(ph).first();
        const report = await env.DB.prepare(`SELECT r.blocked_days,r.blocked_until,r.note,u.username reporter FROM number_reports r JOIN users u ON u.id=r.reporter_user_id WHERE r.phone_hash=? AND r.active=1 AND r.blocked_until>datetime('now') ORDER BY r.blocked_until DESC LIMIT 1`).bind(ph).first();
        const phone = String(body.phone || "").replace(/[^+\d]/g, "").slice(0, 24);
        await env.DB.prepare("INSERT OR IGNORE INTO activations(activation_id,user_id,phone_hash,phone_mask,phone_number,price,status,updated_at) VALUES(?,?,?,?,?,?,'active',datetime('now'))")
          .bind(String(body.activation_id), user.id, ph, mask, phone, Number(body.price || 0)).run();
        await env.DB.prepare("UPDATE activations SET phone_number=?,status='active',updated_at=datetime('now') WHERE activation_id=? AND user_id=?")
          .bind(phone, String(body.activation_id), user.id).run();
        return json({ duplicate_count_7d: history?.count || 0, last_otp: history?.last_otp || null, block_report: report || null });
      }
      if (request.method === "POST" && path === "/activations/success") {
        const otpCode = String(body.otp_code || "").replace(/\s/g, "").slice(0, 32);
        await env.DB.prepare(`UPDATE activations SET
          otp_received_at=COALESCE(otp_received_at,datetime('now')),
          otp_count=otp_count+CASE WHEN otp_received_at IS NULL THEN 1 ELSE 0 END,
          otp_code=?,status='success',updated_at=datetime('now')
          WHERE activation_id=? AND user_id=?`).bind(otpCode, String(body.activation_id), user.id).run();
        return json({ ok: true });
      }
      if (request.method === "POST" && path === "/activations/status") {
        const status = String(body.status || "");
        if (!new Set(["active", "completed", "cancelled", "expired"]).has(status)) {
          return json({ error: "invalid_activation_status" }, 400);
        }
        await env.DB.prepare("UPDATE activations SET status=?,updated_at=datetime('now') WHERE activation_id=? AND user_id=?")
          .bind(status, String(body.activation_id), user.id).run();
        return json({ ok: true });
      }
      if (request.method === "POST" && path === "/numbers/report") {
        const days = Math.min(365, Math.max(1, Number(body.days || 0))); const ph = await phoneHash(env, body.phone);
        await env.DB.prepare(`INSERT INTO number_reports(phone_hash,phone_mask,reporter_user_id,blocked_days,blocked_until,note) VALUES(?,?,?,?,datetime('now',? || ' days'),?)`)
          .bind(ph, maskPhone(body.phone), user.id, days, `+${days}`, String(body.note || "").slice(0, 200)).run();
        return json({ ok: true, days });
      }
      return json({ error: "not_found" }, 404);
    } catch (error) { return json({ error: String(error?.message || error) }, 500); }
  }
};
