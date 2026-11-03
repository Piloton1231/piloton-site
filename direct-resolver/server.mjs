import http from "node:http";

import { GET as resolveYouTube } from "./api/resolve.mjs";

const port = Number.parseInt(process.env.PORT || "8000", 10);

function sendJson(response, status, body) {
  const data = JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  });
  response.end(data);
}

const server = http.createServer(async (request, response) => {
  if ((request.url || "").length > 4096) {
    sendJson(response, 414, { detail: "Request URL is too long" });
    return;
  }
  const requestUrl = new URL(
    request.url || "/",
    "http://localhost",
  );

  if (request.method !== "GET") {
    sendJson(response, 405, { detail: "Method not allowed" });
    return;
  }

  if (requestUrl.pathname === "/health") {
    sendJson(response, 200, {
      status: "ok",
      resolver: "youtubejs-pot",
      proxyEnabled: Boolean((process.env.YOUTUBE_PROXY_URL || "").trim()),
    });
    return;
  }

  if (requestUrl.pathname !== "/resolve") {
    sendJson(response, 404, { detail: "Not found" });
    return;
  }

  try {
    const result = await resolveYouTube(
      new Request(requestUrl, {
        headers: {
          "X-Piloton-Client-IP": request.socket.remoteAddress || "unknown",
        },
      }),
    );
    response.writeHead(
      result.status,
      Object.fromEntries(result.headers.entries()),
    );
    if (result.body) {
      response.end(Buffer.from(await result.arrayBuffer()));
    } else {
      response.end();
    }
  } catch (error) {
    console.error("Request failed", error?.name || "Error");
    sendJson(response, 500, { detail: "Internal server error" });
  }
});

server.maxHeadersCount = 64;
server.headersTimeout = 10_000;
server.keepAliveTimeout = 5_000;
server.requestTimeout = 65_000;
server.listen(port, "0.0.0.0", () => {
  console.log(`Piloton direct resolver listening on port ${port}`);
});
