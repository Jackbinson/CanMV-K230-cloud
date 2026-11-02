# Hướng dẫn chạy phép thử ping Apps Script trên CanMV K230

## Mục đích

Tệp `k230_drive_ping_restart.py` chỉ gửi yêu cầu `ping` tới Apps Script để kiểm tra đường truyền và `device_token`. Tệp **không tải video lên Drive** và không sửa cấu hình trên thẻ. Sau khi thử xong, tệp tự khởi động lại board.

## Trước khi chạy

- Camera đang kết nối Wi-Fi và màn hình có địa chỉ IP.
- Apps Script đã triển khai dạng Web app; URL trong cấu hình phải là URL kết thúc bằng `/exec`.
- Thẻ có file cấu hình tại:

  ```text
  /sdcard/recorder/drive_upload_config.json
  ```

- File cấu hình cần có các khóa `webapp_url` và `device_token`. Giữ token bí mật, không chụp hoặc gửi file cấu hình cho người khác.
- Chạy lúc camera không quay clip. Khi CanMV kết nối hoặc khi script reset board, chương trình đang chạy có thể bị ngắt.
- Tệp `/sdcard/recorder/wifi_client.py` cần có trên thẻ để script tự bật Wi-Fi từ cấu hình đã lưu; helper chỉ đọc các file Wi-Fi hiện có và không in mật khẩu.

## Các bước trong CanMV IDE

1. Cắm camera với cáp USB dữ liệu và mở CanMV IDE.
2. Bấm biểu tượng kết nối ở góc dưới bên trái. Nếu được hỏi cổng, chọn COM của camera (trước đây là `COM3`). Xác nhận thanh trạng thái hiện board `Canmv-K230-Yahboom-1G` và đúng cổng COM.
3. Mở tab `k230_drive_ping_restart.py`. Nếu chưa có tab, chọn **File → Open** và mở tệp:

   ```text
   K230_AppsScript_Drive/k230_drive_ping_restart.py
   ```

   Tệp nằm trong thư mục dự án `Model của dataset traffic` trên OneDrive.

4. Bấm nút tam giác xanh **Start / Run** ở góc dưới bên trái. Không chọn **Tools → Save open script to CanMV board (as main.py)**; thao tác đó sẽ thay chương trình khởi động của camera.
5. Mở vùng **Serial Terminal** ở cạnh dưới IDE và chờ kết quả.

## Cơ chế kiểm tra kết nối

`k230_check_http_modules.py` chỉ kiểm tra module có sẵn, **không kết nối mạng, không đọc token và không gửi dữ liệu**. Bản ping dùng TLS socket để đọc status và theo redirect của Apps Script, vì API `requests` trong firmware không hỗ trợ `allow_redirects`. Trước khi ping, script gọi `/sdcard/recorder/wifi_client.py` để kết nối Wi-Fi bằng cấu hình đã lưu trên thẻ.

Script gửi một POST thăm dò **không có token** trước. Chỉ khi nhận đúng phản hồi `unauthorized` từ Apps Script, script mới gửi ping có `device_token`. Nếu POST thăm dò không nhận được status HTTP thì token không được gửi trong lần chạy đó.

**Giới hạn bảo mật:** firmware `requests` dùng `CERT_NONE`; bản ping hiện giữ cùng chế độ TLS này để tương thích. Kết nối được mã hóa nhưng chưa xác minh chứng thư máy chủ. Chỉ dùng cho phép ping chẩn đoán; trước khi bật gửi dữ liệu tự động, cần cấu hình CA tin cậy và xác minh chứng thư.

## Đọc kết quả

- `APPS_SCRIPT_PING_OK` — Apps Script đã trả lời và chấp nhận ping.
- `APPS_SCRIPT_PREFLIGHT_OK no-token unauthorized response` — endpoint đã trả lời đúng; script tiếp tục ping có token.
- `APPS_SCRIPT_PING_FAILED ...` trước dòng preflight — lỗi ở bước thăm dò; lần chạy này chưa gửi token.
- `APPS_SCRIPT_PING_FAILED ...` sau dòng preflight — lỗi trong ping có token; giữ nguyên nguyên văn phần lỗi để kiểm tra tiếp.
- `FileNotFoundError` — thiếu file cấu hình tại `/sdcard/recorder/drive_upload_config.json`.
- Lỗi khóa `webapp_url` hoặc `device_token` — tên khóa trong JSON chưa đúng hoặc bị thiếu.
- Timeout/lỗi mạng — kiểm tra Wi-Fi, IP và Internet trên mạng đang dùng.
- HTTP 401/403 hoặc `rejected ping` — kiểm tra `device_token` trên board có trùng với Script Property `DEVICE_TOKEN` trong Apps Script không.
- HTTP 404 — kiểm tra URL `/exec` và trạng thái triển khai Web app.

## Sau phép thử

Script đợi khoảng 1,2 giây rồi gọi `machine.reset()` **kể cả khi ping thất bại**. Board sẽ khởi động lại; kiểm tra màn hình để biết chương trình camera/model đã chạy lại chưa. Nếu chưa, không lưu script ping thành `main.py`; mở lại chương trình camera gốc theo quy trình hiện có.
