export function GET() {
  return Response.json({
    status: "ok",
    resolver: "youtubejs-pot",
    proxyEnabled: Boolean((process.env.YOUTUBE_PROXY_URL || "").trim()),
  });
}
