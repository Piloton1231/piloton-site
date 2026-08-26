import { BotGuardClient } from "bgutils-js/botguard";
import { WebPoMinter } from "bgutils-js/webpo";
import {
  USER_AGENT,
  buildURL,
  getHeaders,
  parseLooseJSON,
} from "bgutils-js/utils";
import { JSDOM } from "jsdom";
import Innertube, { Platform, UniversalCache } from "youtubei.js";

const ALLOWED_HOSTS = new Set([
  "youtube.com",
  "www.youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtu.be",
]);
const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;
const DIRECT_CACHE_SECONDS = 180;
const RATE_LIMIT = 12;
const RATE_WINDOW_MS = 60_000;
const directCache = new Map();
const requestsByClient = new Map();
let resolverStatePromise;

Platform.shim.eval = async (data) => new Function(data.output)();

function parseVideoId(value) {
  if (typeof value !== "string" || value.length < 1 || value.length > 2048) {
    throw new TypeError("A YouTube URL is required");
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError("Invalid URL");
  }

  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (
    parsed.protocol !== "https:" ||
    !ALLOWED_HOSTS.has(host) ||
    parsed.username ||
    parsed.password ||
    parsed.port
  ) {
    throw new TypeError("Only HTTPS YouTube URLs are allowed");
  }

  const parts = parsed.pathname.split("/").filter(Boolean);
  let videoId;
  if (host === "youtu.be" && parts.length === 1) {
    [videoId] = parts;
  } else if (parsed.pathname === "/watch") {
    videoId = parsed.searchParams.get("v");
  } else if (
    parts.length === 2 &&
    ["shorts", "live", "embed"].includes(parts[0])
  ) {
    videoId = parts[1];
  }

  if (!videoId || !VIDEO_ID_PATTERN.test(videoId)) {
    throw new TypeError("A YouTube video URL is required");
  }
  return videoId;
}

function checkRateLimit(request) {
  const forwarded = request.headers["x-forwarded-for"];
  const client = String(Array.isArray(forwarded) ? forwarded[0] : forwarded || "unknown")
    .split(",", 1)[0]
    .trim();
  const now = Date.now();
  const recent = (requestsByClient.get(client) || []).filter(
    (timestamp) => timestamp > now - RATE_WINDOW_MS,
  );
  if (recent.length >= RATE_LIMIT) {
    throw new RangeError("Too many requests");
  }
  recent.push(now);
  requestsByClient.set(client, recent);
}

function installDom(ytConfig) {
  const dom = new JSDOM(
    "<!DOCTYPE html><html lang=\"en\"><head><title></title></head><body></body></html>",
    {
      url: "https://www.youtube.com/",
      referrer: "https://www.youtube.com/",
      userAgent: USER_AGENT,
    },
  );
  const yt = { config_: ytConfig };
  dom.window.yt = yt;
  Object.assign(globalThis, {
    yt,
    window: dom.window,
    document: dom.window.document,
    location: dom.window.location,
    origin: dom.window.origin,
  });
  if (!("navigator" in globalThis)) {
    Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator });
  }
}

async function createResolverState() {
  const pageResponse = await fetch("https://www.youtube.com", {
    headers: {
      accept: "*/*",
      "accept-language": "en-US,en;q=0.7",
      "user-agent": USER_AGENT,
    },
  });
  if (!pageResponse.ok) {
    throw new Error(`YouTube homepage returned ${pageResponse.status}`);
  }
  const pageHtml = await pageResponse.text();
  const ytConfigMatch = pageHtml.match(/ytcfg\.set\(({.+?})\);/s);
  const attestationMatch = pageHtml.match(/window\.ytAtN\(\s*({[\s\S]*?})\s*\)/);
  if (!ytConfigMatch || !attestationMatch) {
    throw new Error("YouTube attestation data is missing");
  }

  const ytConfig = JSON.parse(ytConfigMatch[1]);
  const attestationData = parseLooseJSON(attestationMatch[1]);
  const challenge = attestationData?.R?.bgChallenge;
  const interpreterPath =
    challenge?.interpreterUrl?.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
  if (!challenge?.program || !challenge?.globalName || !interpreterPath) {
    throw new Error("YouTube BotGuard challenge is incomplete");
  }

  installDom(ytConfig);
  const interpreterResponse = await fetch(`https:${interpreterPath}`);
  if (!interpreterResponse.ok) {
    throw new Error(`BotGuard interpreter returned ${interpreterResponse.status}`);
  }
  const interpreterJavascript = await interpreterResponse.text();
  new Function(interpreterJavascript)();

  const botGuardClient = await BotGuardClient.create({
    program: challenge.program,
    globalName: challenge.globalName,
    globalObject: globalThis,
  });
  const webPoSignalOutput = [];
  const botguardResponse = await botGuardClient.snapshot({ webPoSignalOutput });
  const integrityResponse = await fetch(buildURL("GenerateIT", true), {
    method: "POST",
    headers: getHeaders(),
    body: JSON.stringify(["O43z0dpjhgX20SCx4KAo", botguardResponse]),
  });
  if (!integrityResponse.ok) {
    throw new Error(`Integrity service returned ${integrityResponse.status}`);
  }
  const [integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken] =
    await integrityResponse.json();
  if (!integrityToken) {
    throw new Error("Integrity service did not return a token");
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
  const innertube = await Innertube.create({ cache: new UniversalCache(false) });
  return {
    expiresAt: Date.now() + Math.max(60, estimatedTtlSecs - 60) * 1000,
    innertube,
    minter,
  };
}

async function getResolverState() {
  if (!resolverStatePromise) {
    resolverStatePromise = createResolverState().catch((error) => {
      resolverStatePromise = undefined;
      throw error;
    });
  }
  const state = await resolverStatePromise;
  if (state.expiresAt <= Date.now()) {
    resolverStatePromise = undefined;
    return getResolverState();
  }
  return state;
}

export async function resolveDirectUrl(value) {
  const videoId = parseVideoId(value);
  const cached = directCache.get(videoId);
  if (cached && cached.expiresAt > Date.now()) {
    return { cached: true, url: cached.url };
  }

  const state = await getResolverState();
  const poToken = await state.minter.mintAsWebsafeString(videoId);
  const videoInfo = await state.innertube.getBasicInfo(videoId, {
    client: "MWEB",
    po_token: poToken,
  });
  const format = videoInfo.chooseFormat({ itag: 18 });
  const deciphered = await format.decipher(state.innertube.session.player);
  const mediaUrl = new URL(deciphered);
  const mediaHost = mediaUrl.hostname.toLowerCase().replace(/\.$/, "");
  if (mediaUrl.protocol !== "https:" || !mediaHost.endsWith(".googlevideo.com")) {
    throw new Error("Unexpected media host");
  }
  mediaUrl.searchParams.set("pot", poToken);
  const directUrl = mediaUrl.toString();
  directCache.set(videoId, {
    expiresAt: Date.now() + DIRECT_CACHE_SECONDS * 1000,
    url: directUrl,
  });
  return { cached: false, url: directUrl };
}

export default async function handler(request, response) {
  if (request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return response.status(405).json({ detail: "Method not allowed" });
  }

  try {
    checkRateLimit(request);
    const result = await resolveDirectUrl(request.query.url);
    response.setHeader("Cache-Control", "no-store");
    response.setHeader(
      "X-Resolver-Path",
      result.cached ? "direct-googlevideo-cache" : "direct-googlevideo",
    );
    return response.redirect(307, result.url);
  } catch (error) {
    console.error("YouTube direct resolver failed", error);
    response.setHeader("Cache-Control", "no-store");
    if (error instanceof TypeError) {
      return response.status(400).json({ detail: error.message });
    }
    if (error instanceof RangeError) {
      return response.status(429).json({ detail: error.message });
    }
    return response.status(502).json({ detail: "Could not resolve the video" });
  }
}
