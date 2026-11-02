# Relay miễn phí cho CanMV K230 → Apps Script

## Cập nhật Apps Script trước

Phản hồi PowerShell hiện tại là `200 OK` với `Content-Type: text/html`; đó là trang HTML do `HtmlService` tạo, trong đó JSON chỉ được nhúng vào trang. API cần trả JSON thuần. Mở `K230_AppsScript_Drive/Code.gs` trong repo, chép toàn bộ nội dung vào tệp `Code.gs` của Apps Script, rồi chọn **Triển khai → Quản lý các tùy chọn triển khai → Chỉnh sửa → Phiên bản mới → Triển khai**. Không sửa bằng cách chỉ chạy hàm trong trình biên tập; phải triển khai phiên bản web app mới.

Sau khi triển khai, khi kiểm tra trực tiếp bằng PowerShell, lệnh `curl.exe -i -L "<web-app-url>/exec?action=nonce"` cần trả nội dung JSON thuần, ví dụ `{"ok":true,"request_id":"..."}`. Lệnh không có `-L` có thể chỉ hiện phản hồi chuyển hướng của `ContentService`; Worker sẽ tự theo chuyển hướng này.

## Vì sao dùng relay

Apps Script ghi nhận K230 gọi `doGet`, nhưng firmware CanMV không đọc được dòng HTTP trả về. Worker nhận yêu cầu của camera, gọi URL Apps Script từ máy chủ Cloudflare, theo redirect của Google rồi trả kết quả cuối cho camera.

Worker chỉ chuyển các yêu cầu điều khiển nhỏ (`nonce`, `result`, `ping`, `init`). Video vẫn được K230 tải trực tiếp lên URL resumable của Google Drive; Worker không nhận file MP4.

## Tạo Worker

1. Tạo tài khoản Cloudflare Free và tạo một Worker mới.
2. Mở phần sửa mã của Worker, thay mã mẫu bằng `worker.js`, rồi Deploy.
3. Trong cài đặt Worker, thêm hai biến:

   - `APPS_SCRIPT_EXEC_URL`: URL Web app Apps Script kết thúc bằng `/exec`.
   - `RELAY_PATH`: chuỗi ngẫu nhiên dài, chỉ gồm chữ số và chữ cái hex. Tạo trên PowerShell bằng:

   ```powershell
   ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')).ToLower()
   ```

   Đặt `RELAY_PATH` ở dạng secret nếu giao diện cho phép. Không gửi giá trị này qua chat.

4. URL Worker sẽ có dạng:

   ```text
   https://TEN-WORKER.TEN-TAI-KHOAN.workers.dev/GIA-TRI-RELAY_PATH/exec
   ```

5. Mở URL Worker đó và thêm `?action=nonce`. Kết quả cần là JSON có `ok: true` và `request_id`.
6. Đổi `webapp_url` trong `/sdcard/recorder/drive_upload_config.json` sang URL Worker ở bước 4. Giữ nguyên `device_token`.
7. Chạy `k230_drive_ping_poll.py` trên CanMV. Chỉ thử uploader sau khi ping thành công.

## Giới hạn và bảo mật

Theo giới hạn Workers hiện được Cloudflare công bố, gói Free cho phép tối đa 100.000 request/ngày. Mỗi clip chỉ cần vài request điều khiển qua Worker; dữ liệu video không đi qua Worker. [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)

Worker chỉ chuyển tiếp tới URL Apps Script đã cấu hình, chỉ nhận GET/POST và chỉ hoạt động trên đường dẫn bí mật `RELAY_PATH`. Không ghi token hoặc nội dung request vào log. Giữ bí mật `RELAY_PATH`, URL Worker đầy đủ và `device_token`.
