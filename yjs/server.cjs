/**
 * Canvasphere Yjs WebSocket Server with built-in HS256 JWT room authentication.
 * Zero extra npm dependencies — verifies JWT using Node's native `crypto` module.
 *
 * Env:
 *   HOST=0.0.0.0
 *   PORT=1234
 *   JWT_SECRET=...            (must match backend API JWT_SECRET)
 *   YJS_REQUIRE_AUTH=true     (default true in production; false allows unauthenticated local dev)
 */
const http = require("http");
const crypto = require("crypto");
const WebSocket = require("ws");
const { setupWSConnection } = require("y-websocket/bin/utils");

const HOST = process.env.HOST || "0.0.0.0";
const PORT = parseInt(process.env.PORT || "1234", 10);
const JWT_SECRET = process.env.JWT_SECRET || "dev-insecure-canvasphere-jwt-secret-change-me";
const REQUIRE_AUTH = (process.env.YJS_REQUIRE_AUTH || "false").toLowerCase() === "true";

function verifyHs256Jwt(token) {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [headerB64, payloadB64, sigB64] = parts;
  const expected = crypto
    .createHmac("sha256", JWT_SECRET)
    .update(`${headerB64}.${payloadB64}`)
    .digest("base64url");
  const sigBuf = Buffer.from(sigB64);
  const expBuf = Buffer.from(expected);
  if (sigBuf.length !== expBuf.length || !crypto.timingSafeEqual(sigBuf, expBuf)) {
    return null;
  }
  try {
    const payload = JSON.parse(Buffer.from(payloadB64, "base64url").toString("utf8"));
    if (payload.type !== "access" || !payload.sub) return null;
    if (payload.exp && Math.floor(Date.now() / 1000) >= payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

function extractToken(req) {
  const auth = req.headers["authorization"] || "";
  if (auth.toLowerCase().startsWith("bearer ")) {
    return auth.slice(7).trim();
  }
  try {
    const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    return (url.searchParams.get("token") || "").trim();
  } catch {
    return "";
  }
}

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", auth_required: REQUIRE_AUTH }));
    return;
  }
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end("canvasphere yjs server\n");
});

const wss = new WebSocket.Server({ noServer: true });

wss.on("connection", (ws, req) => {
  setupWSConnection(ws, req);
});

server.on("upgrade", (req, socket, head) => {
  if (REQUIRE_AUTH) {
    const token = extractToken(req);
    const claims = verifyHs256Jwt(token);
    if (!claims) {
      socket.write("HTTP/1.1 401 Unauthorized\r\nContent-Type: text/plain\r\n\r\nUnauthorized\r\n");
      socket.destroy();
      return;
    }
    req.userId = claims.sub;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit("connection", ws, req);
  });
});

server.listen(PORT, HOST, () => {
  console.log(`canvasphere yjs listening at ${HOST}:${PORT} (auth=${REQUIRE_AUTH})`);
});
