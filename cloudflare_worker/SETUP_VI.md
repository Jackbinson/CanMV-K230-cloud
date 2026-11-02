# Relay miễn phí cho CanMV K230 → Apps Script

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
