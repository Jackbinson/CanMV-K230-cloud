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

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return json_({ ok: false, error: "empty_request" });
    }

    const request = JSON.parse(e.postData.contents);
    if (!authorized_(request.token)) {
      return json_({ ok: false, error: "unauthorized" });
    }

    if (request.action === "ping") {
      return json_({ ok: true, service: "k230-drive-upload" });
    }
    if (request.action !== "init") {
      return json_({ ok: false, error: "unsupported_action" });
    }

    const name = safeFileName_(request.name);
    const cameraId = String(request.camera_id || "");
    const size = Number(request.size);
    if (!/^[A-Za-z0-9_-]{1,32}$/.test(cameraId)) {
      return json_({ ok: false, error: "invalid_camera_id" });
    }
    if (!Number.isSafeInteger(size) || size < 1 || size > MAX_FILE_BYTES) {
      return json_({ ok: false, error: "invalid_file_size" });
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
      return json_({ ok: false, error: "drive_session_failed", http_status: code });
    }

    // This URL is a temporary bearer capability. Return it only after token
    // validation and never write it to logs or Script Properties.
    return json_({ ok: true, upload_url: uploadUrl, name: name, size: size });
  } catch (err) {
    console.error("Request failed: " + String(err));
    return json_({ ok: false, error: "server_error" });
  }
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
