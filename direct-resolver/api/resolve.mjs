import { BotGuardClient } from "bgutils-js/botguard";
import {
  buildURL,
  getHeaders,
  parseLooseJSON,
  USER_AGENT,
} from "bgutils-js/utils";
import { WebPoMinter } from "bgutils-js/webpo";
import { JSDOM } from "jsdom";
import { Innertube, Platform } from "youtubei.js";

Platform.shim.eval = async (data) => new Function(data.output)();

const REQUEST_KEY = "O43z0dpjhgX20SCx4KAo";
const YOUTUBE_HOSTS = new Set([
  "youtube.com",
  "www.youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtu.be",
]);
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

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
      },
    },
  );
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
  const homepage = await homepageResponse.text();

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

  const interpreterResponse = await fetch(`https:${interpreterPath}`);
  if (!interpreterResponse.ok) {
    throw new Error(
      `BotGuard interpreter returned ${interpreterResponse.status}`,
    );
  }
  const interpreterJavascript = await interpreterResponse.text();
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
        "X-Resolver-Path": "direct-googlevideo",
      },
    });
  } catch (error) {
    console.error("Direct resolver failed", error);
    return jsonError(502, "YouTube direct URL extraction failed");
  }
}
