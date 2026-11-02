/**
 * Minimal, token-protected Apps Script endpoint for starting Google Drive
 * resumable uploads. Video bytes go directly from the K230 to Drive; they do
 * not pass through Apps Script.
 *
 * Script Properties required:
 *   DEVICE_TOKEN   Long random value shared with the K230 config file.
 *   DRIVE_FOLDER_ID Optional. If absent, the script creates K230_Clips once.
 */
const DRIVE_API = "https://www.googleapis.com/drive/v3/files";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";
const MAX_FILE_BYTES = 256 * 1024 * 1024;
const RPC_TTL_SECONDS = 600;

// K230 obtains a short-lived random request ID with GET, sends the operation
// with POST, then retrieves the result with GET. This avoids depending on the
// K230 receiving Apps Script's POST response directly.
function doGet(e) {
  const parameters = e && e.parameter ? e.parameter : {};
  const action = String(parameters.action || "");
  const cache = CacheService.getScriptCache();

  if (action === "nonce") {
    const requestId = Utilities.getUuid().replace(/-/g, "").toLowerCase();
    cache.put(pendingKey_(requestId), "1", RPC_TTL_SECONDS);
    return htmlJson_({ ok: true, request_id: requestId });
  }

  if (action === "result") {
    const requestId = String(parameters.id || "");
    if (!validRequestId_(requestId)) return htmlJson_({ ok: false, error: "invalid_request_id" });
    const resultText = cache.get(resultKey_(requestId));
    if (!resultText) return htmlJson_({ ok: false, pending: true });
    return HtmlService.createHtmlOutput(resultText).setTitle("K230 API");
  }

  return htmlJson_({ ok: false, error: "unsupported_get_action" });
}

function doPost(e) {
  let requestId = "";
  let result;
  try {
    if (!e || !e.postData || !e.postData.contents) {
      result = { ok: false, error: "empty_request" };
    } else {
      const request = JSON.parse(e.postData.contents);
      requestId = String(request.request_id || "");
      result = handleRequest_(request);
    }
  } catch (err) {
    console.error("Request failed: " + String(err));
    result = { ok: false, error: "server_error" };
  }

  if (validRequestId_(requestId)) {
    const cache = CacheService.getScriptCache();
    const pendingKey = pendingKey_(requestId);
    if (cache.get(pendingKey) === "1") {
      cache.remove(pendingKey);
      cache.put(resultKey_(requestId), JSON.stringify(result), RPC_TTL_SECONDS);
      return json_({ ok: true, queued: true });
    }
  }
  return json_(result);
}

function handleRequest_(request) {
  if (!authorized_(request.token)) return { ok: false, error: "unauthorized" };

  if (request.action === "ping") {
    return { ok: true, service: "k230-drive-upload" };
  }
  if (request.action !== "init") {
    return { ok: false, error: "unsupported_action" };
  }

  const name = safeFileName_(request.name);
  const cameraId = String(request.camera_id || "");
  const size = Number(request.size);
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(cameraId)) {
    return { ok: false, error: "invalid_camera_id" };
  }
  if (!Number.isSafeInteger(size) || size < 1 || size > MAX_FILE_BYTES) {
    return { ok: false, error: "invalid_file_size" };
  }

  const folderId = getOrCreateFolder_();
  const mimeType = name.toLowerCase().endsWith(".mp4")
    ? "video/mp4"
    : "application/json";
  const metadata = {
    name: name,
    mimeType: mimeType,
    parents: [folderId],
    appProperties: {
      camera_id: cameraId,
      source: "canmv-k230"
    }
  };

  const response = UrlFetchApp.fetch(
    DRIVE_UPLOAD_API + "?uploadType=resumable&fields=id,name,mimeType",
    {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(metadata),
      headers: {
        Authorization: "Bearer " + ScriptApp.getOAuthToken(),
        "X-Upload-Content-Type": mimeType,
        "X-Upload-Content-Length": String(size)
      },
      muteHttpExceptions: true
    }
  );

  const code = response.getResponseCode();
  const headers = response.getAllHeaders();
  const uploadUrl = headerValue_(headers, "Location");
  if (code < 200 || code >= 300 || !uploadUrl) {
    console.error("Drive upload-session creation failed; HTTP " + code);
    return { ok: false, error: "drive_session_failed", http_status: code };
  }

  // This URL is a temporary bearer capability. It is kept in short-lived
  // Script Cache and returned only to the caller that holds the request ID.
  return { ok: true, upload_url: uploadUrl, name: name, size: size };
}

function validRequestId_(requestId) {
  return /^[a-f0-9]{32}$/.test(String(requestId || ""));
}

function pendingKey_(requestId) {
  return "K230_PENDING_" + requestId;
}

function resultKey_(requestId) {
  return "K230_RESULT_" + requestId;
}

function authorized_(candidate) {
  const expected = PropertiesService.getScriptProperties().getProperty("DEVICE_TOKEN");
  return !!expected && typeof candidate === "string" && candidate.length >= 32 &&
    candidate === expected;
}

function safeFileName_(value) {
  if (typeof value !== "string" || value.length > 100 ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.(mp4|json)$/i.test(value)) {
    throw new Error("invalid_file_name");
  }
  return value;
}

function getOrCreateFolder_() {
  const properties = PropertiesService.getScriptProperties();
  const savedId = properties.getProperty("DRIVE_FOLDER_ID");
  if (savedId) return savedId;

  const response = UrlFetchApp.fetch(DRIVE_API + "?fields=id,name,mimeType", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ name: "K230_Clips", mimeType: "application/vnd.google-apps.folder" }),
    headers: { Authorization: "Bearer " + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true
  });
  const code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    console.error("Drive folder creation failed; HTTP " + code);
    throw new Error("drive_folder_creation_failed");
  }
  const folder = JSON.parse(response.getContentText());
  if (!folder.id) throw new Error("drive_folder_id_missing");
  properties.setProperty("DRIVE_FOLDER_ID", folder.id);
  return folder.id;
}

function headerValue_(headers, wantedName) {
  const wanted = wantedName.toLowerCase();
  const keys = Object.keys(headers || {});
  for (let i = 0; i < keys.length; i++) {
    if (keys[i].toLowerCase() === wanted) return String(headers[keys[i]]);
  }
  return "";
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function htmlJson_(value) {
  // GET replies use HtmlService rather than ContentService to avoid its
  // one-time script.googleusercontent.com redirect on this CanMV client.
  return HtmlService.createHtmlOutput(JSON.stringify(value)).setTitle("K230 API");
}
