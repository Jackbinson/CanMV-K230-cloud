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

`k230_check_http_modules.py` chỉ kiểm tra module có sẵn, **không kết nối mạng, không đọc token và không gửi dữ liệu**. Bản ping mới dùng TLS socket trực tiếp. Vì firmware nhận POST ở Apps Script nhưng không đọc được phản hồi POST, script xin một request ID ngẫu nhiên bằng GET, gửi POST có token, rồi đọc kết quả bằng GET. Request ID có thời hạn ngắn; token không nằm trong URL. Trước khi ping, script gọi `/sdcard/recorder/wifi_client.py` để kết nối Wi-Fi bằng cấu hình đã lưu trên thẻ.

Phải cập nhật `Code.gs` trong Apps Script và triển khai phiên bản Web app mới trước khi chạy script này. Nếu vẫn dùng phiên bản cũ, các yêu cầu `action=nonce` và `action=result` sẽ báo `unsupported_get_action`.

**Giới hạn bảo mật:** bản ping dùng TLS với `CERT_NONE` để tương thích firmware. Kết nối được mã hóa nhưng chưa xác minh chứng thư máy chủ. Không gửi video hoặc dữ liệu nhạy cảm qua bản thử nghiệm này.

## Đọc kết quả

Trên firmware CanMV, `read()` trả về `b''` cả khi hết thời gian chờ, khi bị reset và khi server đóng kết nối, và trả `None` khi lớp TLS chưa có dữ liệu. Vì vậy một lần đọc rỗng **không** có nghĩa là server đã trả lời xong. Script đọc tiếp cho tới khi nhận đủ header và body (theo `Content-Length` hoặc chunked), tối đa 30 giây cho mỗi yêu cầu.

- `RAW_HTTPS_FIRST_BYTE_MS <ms>` — thời gian từ lúc bắt đầu đọc tới khi nhận byte phản hồi đầu tiên.
- `RAW_HTTPS_EMPTY_READ <None hoặc b''> <ms>` — một lần đọc rỗng (in tối đa 5 dòng mỗi yêu cầu). Script vẫn tiếp tục đọc.
- `RAW_HTTPS_READ_GAVE_UP <số lần> <ms>` — hết 30 giây mà phản hồi chưa đủ.
- `APPS_SCRIPT_POST_STATUS 302` — K230 đã đọc được phản hồi POST. Nếu dòng này xuất hiện thì lỗi `bad status line: b''` cũ đã được khắc phục.
- Khi báo kết quả, gửi **toàn bộ** các dòng `RAW_HTTPS_...` kèm số ms. Con số này cho biết lỗi do hết thời gian chờ (khoảng 20 000 ms), do kết nối bị reset (gần 0 ms với `b''`) hay chỉ là TLS chưa sẵn sàng (`None`).
- `APPS_SCRIPT_PING_OK` — Apps Script đã trả lời qua GET polling và chấp nhận ping có token.
- `APPS_SCRIPT_RESULT_PENDING ...` — script đang đợi Apps Script ghi kết quả vào cache.
- `APPS_SCRIPT_PING_FAILED Apps Script did not issue a request ID` — kiểm tra đã dán `Code.gs` mới và triển khai đúng phiên bản Web app.
- `APPS_SCRIPT_PING_FAILED ...` — giữ nguyên toàn bộ log để xác định lỗi ở bước lấy ID, gửi POST hay đọc kết quả GET.
- `FileNotFoundError` — thiếu file cấu hình tại `/sdcard/recorder/drive_upload_config.json`.
- Lỗi khóa `webapp_url` hoặc `device_token` — tên khóa trong JSON chưa đúng hoặc bị thiếu.
- Timeout/lỗi mạng — kiểm tra Wi-Fi, IP và Internet trên mạng đang dùng.
- HTTP 401/403 hoặc `rejected ping` — kiểm tra `device_token` trên board có trùng với Script Property `DEVICE_TOKEN` trong Apps Script không.
- HTTP 404 — kiểm tra URL `/exec` và trạng thái triển khai Web app.

## Sau phép thử

Script đợi khoảng 1,2 giây rồi gọi `machine.reset()` **kể cả khi ping thất bại**. Board sẽ khởi động lại; kiểm tra màn hình để biết chương trình camera/model đã chạy lại chưa. Nếu chưa, không lưu script ping thành `main.py`; mở lại chương trình camera gốc theo quy trình hiện có.
