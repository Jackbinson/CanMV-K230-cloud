/**
 * Small control-plane relay for CanMV K230.
 *
 * The K230 calls this Worker with GET/POST. The Worker forwards only those
 * small control requests to the fixed Apps Script web app and follows Google's
 * ContentService redirects, returning the final response directly to the K230.
 * MP4 bytes still upload directly from the K230 to the Drive resumable URL.
 *
 * Worker variables/secrets:
 *   APPS_SCRIPT_EXEC_URL  The Apps Script deployment URL ending in /exec.
 *   RELAY_PATH           Random private URL segment; never log or share it.
 */
export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);
    const expectedPath = "/" + String(env.RELAY_PATH || "") + "/exec";

    if (!env.APPS_SCRIPT_EXEC_URL || !env.RELAY_PATH || incoming.pathname !== expectedPath) {
      return new Response("not_found", { status: 404 });
    }
    if (request.method !== "GET" && request.method !== "POST") {
      return new Response("method_not_allowed", {
        status: 405,
        headers: { "Allow": "GET, POST", "Cache-Control": "no-store" }
      });
    }

    if (request.method === "GET" &&
        !["nonce", "result"].includes(incoming.searchParams.get("action"))) {
      return new Response("unsupported_get_action", { status: 400 });
    }

    const target = new URL(env.APPS_SCRIPT_EXEC_URL);
    if (target.protocol !== "https:" || target.hostname !== "script.google.com" ||
        !target.pathname.endsWith("/exec")) {
      return new Response("invalid_upstream", { status: 500 });
    }
    target.search = incoming.search;

    const headers = new Headers({ "Accept": "application/json, text/plain, text/html" });
    const contentType = request.headers.get("Content-Type");
    if (contentType) headers.set("Content-Type", contentType);

    const init = {
      method: request.method,
      headers,
      redirect: "follow"
    };
    if (request.method === "POST") {
      // The control payload is tiny JSON; video data never passes through here.
      init.body = await request.text();
    }

    try {
      const upstream = await fetch(target.toString(), init);
      const responseHeaders = new Headers({ "Cache-Control": "no-store" });
      const upstreamType = upstream.headers.get("Content-Type");
      if (upstreamType) responseHeaders.set("Content-Type", upstreamType);
      return new Response(upstream.body, {
        status: upstream.status,
        headers: responseHeaders
      });
    } catch (error) {
      // Do not return the upstream URL or request body in an error response.
      return new Response("upstream_unavailable", {
        status: 502,
        headers: { "Cache-Control": "no-store" }
      });
    }
  }
};
