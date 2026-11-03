import { BotGuardClient } from "bgutils-js/botguard";
import {
  buildURL,
  getHeaders,
  parseLooseJSON,
  USER_AGENT,
} from "bgutils-js/utils";
import { WebPoMinter } from "bgutils-js/webpo";
import { JSDOM } from "jsdom";
import { ProxyAgent, setGlobalDispatcher } from "undici";
import { Innertube, Platform } from "youtubei.js";

Platform.shim.eval = async (data) => new Function(data.output)();

const proxyUrl = (process.env.YOUTUBE_PROXY_URL || "").trim();
if (proxyUrl) {
  let parsedProxy;
  try {
    parsedProxy = new URL(proxyUrl);
  } catch {
    throw new Error("YOUTUBE_PROXY_URL is not a valid URL");
  }
  if (!["http:", "https:", "socks5:"].includes(parsedProxy.protocol)) {
    throw new Error("YOUTUBE_PROXY_URL must use HTTP, HTTPS, or SOCKS5");
  }
  setGlobalDispatcher(new ProxyAgent(parsedProxy));
}

const REQUEST_KEY = "O43z0dpjhgX20SCx4KAo";
const YOUTUBE_HOSTS = new Set([
  "youtube.com",
  "www.youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtu.be",
]);
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const MAX_HOMEPAGE_BYTES = 2_000_000;
const MAX_INTERPRETER_BYTES = 1_000_000;
const RATE_LIMIT = 12;
const RATE_WINDOW_MS = 60_000;
const requestBuckets = new Map();

let domReady = false;
let minterState = null;
let minterPromise = null;
let innertubePromise = null;

function jsonError(status, detail) {
  return Response.json(
    { detail },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "https://piloton.cc",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
      },
    },
  );
}

async function readLimitedText(response, maxBytes) {
  const declaredLength = Number.parseInt(
    response.headers.get("content-length") || "0",
    10,
  );
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    await response.body?.cancel();
    throw new Error("Upstream response is too large");
  }

  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new Error("Upstream response is too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk))).toString("utf8");
}

function requestClientKey(request) {
  const trustedHeader = process.env.VERCEL
    ? request.headers.get("x-vercel-forwarded-for")
    : request.headers.get("x-piloton-client-ip");
  return (trustedHeader || "unknown").split(",", 1)[0].trim().slice(0, 128);
}

function checkRateLimit(request) {
  const now = Date.now();
  const client = requestClientKey(request);
  const recent = (requestBuckets.get(client) || []).filter(
    (timestamp) => timestamp > now - RATE_WINDOW_MS,
  );
  if (recent.length >= RATE_LIMIT) return false;
  recent.push(now);
  requestBuckets.set(client, recent);
  if (requestBuckets.size > 10_000) {
    for (const [key, timestamps] of requestBuckets) {
      if (!timestamps.some((timestamp) => timestamp > now - RATE_WINDOW_MS)) {
        requestBuckets.delete(key);
      }
    }
  }
  return true;
}

function parseVideoId(value) {
  if (!value || value.length > 2048) {
    throw new Error("YouTube URL is required");
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("Invalid URL");
  }

  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (
    parsed.protocol !== "https:" ||
    !YOUTUBE_HOSTS.has(host) ||
    parsed.username ||
    parsed.password ||
    parsed.port
  ) {
    throw new Error("Only HTTPS YouTube URLs are allowed");
  }

  let videoId = null;
  if (host === "youtu.be") {
    const parts = parsed.pathname.split("/").filter(Boolean);
    if (parts.length === 1) videoId = parts[0];
  } else if (parsed.pathname === "/watch") {
    videoId = parsed.searchParams.get("v");
  } else {
    const parts = parsed.pathname.split("/").filter(Boolean);
    if (
      parts.length === 2 &&
      ["shorts", "live", "embed"].includes(parts[0])
    ) {
      videoId = parts[1];
    }
  }

  if (!videoId || !VIDEO_ID_RE.test(videoId)) {
    throw new Error("A valid YouTube video URL is required");
  }
  return videoId;
}

function ensureDom() {
  if (domReady) return;
  const dom = new JSDOM(
    '<!DOCTYPE html><html lang="en"><head><title></title></head><body></body></html>',
    {
      url: "https://www.youtube.com/",
      referrer: "https://www.youtube.com/",
    },
  );
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    location: dom.window.location,
    origin: dom.window.origin,
  });
  if (!Reflect.has(globalThis, "navigator")) {
    Object.defineProperty(globalThis, "navigator", {
      value: dom.window.navigator,
    });
  }
  domReady = true;
}

async function createMinter() {
  ensureDom();

  const homepageResponse = await fetch("https://www.youtube.com/", {
    headers: {
      Accept: "*/*",
      "Accept-Language": "en-US,en;q=0.7",
      "User-Agent": USER_AGENT,
    },
  });
  if (!homepageResponse.ok) {
    throw new Error(`YouTube homepage returned ${homepageResponse.status}`);
  }
  const homepage = await readLimitedText(homepageResponse, MAX_HOMEPAGE_BYTES);

  const ytcfgMatch = homepage.match(/ytcfg\.set\(({.+?})\);/s);
  if (ytcfgMatch) {
    const yt = { config_: JSON.parse(ytcfgMatch[1]) };
    globalThis.yt = yt;
    if (globalThis.window) globalThis.window.yt = yt;
  }

  const attestationMatch = homepage.match(
    /window\.ytAtN\(\s*({[\s\S]*?})\s*\)/,
  );
  if (!attestationMatch) {
    throw new Error("YouTube attestation challenge was not found");
  }
  const challenge = parseLooseJSON(attestationMatch[1])?.R?.bgChallenge;
  const interpreterPath =
    challenge?.interpreterUrl
      ?.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
  if (!challenge?.program || !challenge?.globalName || !interpreterPath) {
    throw new Error("YouTube attestation challenge is incomplete");
  }

  const interpreterUrl = new URL(interpreterPath, "https://www.youtube.com/");
  if (
    interpreterUrl.protocol !== "https:" ||
    !YOUTUBE_HOSTS.has(interpreterUrl.hostname.toLowerCase()) ||
    interpreterUrl.username ||
    interpreterUrl.password ||
    interpreterUrl.port ||
    !interpreterUrl.pathname.startsWith("/s/player/")
  ) {
    throw new Error("BotGuard interpreter URL is not trusted");
  }
  const interpreterResponse = await fetch(interpreterUrl);
  if (!interpreterResponse.ok) {
    throw new Error(
      `BotGuard interpreter returned ${interpreterResponse.status}`,
    );
  }
  const interpreterJavascript = await readLimitedText(
    interpreterResponse,
    MAX_INTERPRETER_BYTES,
  );
  new Function(interpreterJavascript)();

  const botGuard = await BotGuardClient.create({
    program: challenge.program,
    globalName: challenge.globalName,
    globalObject: globalThis,
  });
  const webPoSignalOutput = [];
  const botguardResponse = await botGuard.snapshot({ webPoSignalOutput });
  const integrityResponse = await fetch(buildURL("GenerateIT"), {
    method: "POST",
    headers: getHeaders(),
    body: JSON.stringify([REQUEST_KEY, botguardResponse]),
  });
  if (!integrityResponse.ok) {
    throw new Error(`GenerateIT returned ${integrityResponse.status}`);
  }
  const [
    integrityToken,
    estimatedTtlSecs,
    mintRefreshThreshold,
    websafeFallbackToken,
  ] = await integrityResponse.json();
  if (!integrityToken) {
    throw new Error("GenerateIT returned an empty integrity token");
  }

  const minter = await WebPoMinter.create(
    {
      integrityToken,
      estimatedTtlSecs,
      mintRefreshThreshold,
      websafeFallbackToken,
    },
    webPoSignalOutput,
  );
  return {
    minter,
    expiresAt: Date.now() + Math.max(60, estimatedTtlSecs - 30) * 1000,
  };
}

async function getMinter() {
  if (minterState && minterState.expiresAt > Date.now()) {
    return minterState.minter;
  }
  if (!minterPromise) {
    minterPromise = createMinter()
      .then((state) => {
        minterState = state;
        return state;
      })
      .finally(() => {
        minterPromise = null;
      });
  }
  return (await minterPromise).minter;
}

async function getInnertube() {
  if (!innertubePromise) {
    innertubePromise = Innertube.create({
      generate_session_locally: true,
      retrieve_innertube_config: false,
    }).catch((error) => {
      innertubePromise = null;
      throw error;
    });
  }
  return innertubePromise;
}

async function extractDirectUrl(videoId) {
  const innertube = await getInnertube();
  const strategies = [];

  try {
    const minter = await getMinter();
    const poToken = await minter.mintAsWebsafeString(videoId);
    if (poToken) {
      strategies.push({ client: "MWEB", po_token: poToken });
    }
  } catch (error) {
    console.warn("MWEB PO token setup failed", error?.message || error);
  }

  strategies.push(
    { client: "ANDROID_VR" },
    { client: "ANDROID" },
    { client: "IOS" },
    { client: "TV_EMBEDDED" },
  );

  const failures = [];
  for (const strategy of strategies) {
    let format;
    try {
      format = await innertube.getStreamingData(videoId, {
        itag: 18,
        ...strategy,
      });
    } catch (itagError) {
      try {
        format = await innertube.getStreamingData(videoId, {
          type: "video+audio",
          quality: "best",
          format: "mp4",
          ...strategy,
        });
      } catch (fallbackError) {
        failures.push(
          `${strategy.client}: ${fallbackError?.message || itagError?.message || "failed"}`,
        );
        continue;
      }
    }

    const directUrl = format?.url;
    if (!directUrl) {
      failures.push(`${strategy.client}: no compatible MP4 URL`);
      continue;
    }
    const mediaUrl = new URL(directUrl);
    if (!mediaUrl.hostname.toLowerCase().endsWith(".googlevideo.com")) {
      failures.push(`${strategy.client}: unexpected media host`);
      continue;
    }
    return mediaUrl.href;
  }

  throw new Error(
    failures.length
      ? `All YouTube clients failed (${failures.join("; ")})`
      : "No YouTube client strategy was available",
  );
}

export async function GET(request) {
  if (!checkRateLimit(request)) {
    return jsonError(429, "Too many requests");
  }
  const requestUrl = new URL(request.url);
  let videoId;
  try {
    videoId = parseVideoId(requestUrl.searchParams.get("url"));
  } catch (error) {
    return jsonError(400, error.message);
  }

  try {
    const directUrl = await extractDirectUrl(videoId);
    return new Response(null, {
      status: 307,
      headers: {
        Location: directUrl,
        "Cache-Control": "private, max-age=120",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Resolver-Path": "direct-googlevideo",
      },
    });
  } catch (error) {
    console.error("Direct resolver failed", error?.name || "Error");
    return jsonError(502, "YouTube direct URL extraction failed");
  }
}
