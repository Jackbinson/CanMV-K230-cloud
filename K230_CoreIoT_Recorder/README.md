# K230 recorder: KEY, CoreIoT và tự upload Drive

Bản cập nhật này giữ chương trình quay MP4 của Yahboom K230, thêm điều khiển
qua MQTT/CoreIoT và tự upload khi camera chờ. Clip đã lưu được giữ trên thiết
bị; mất Wi-Fi thì uploader thử lại khi kết nối trở lại. KEY hoặc CoreIoT bắt
đầu/dừng quay; không cần bấm KEY để upload.

## Phạm vi tương thích

Đã thử trên `k230_canmv_yahboom`, MicroPython 1.21.0, CanMV USBDBG 0.4.0.
Đây là gói cập nhật cho recorder có sẵn, không phải firmware hoặc bộ SDK hoàn
chỉnh. Máy cần các module `media`, `ybUtils`, recorder và cấu hình Wi-Fi đang
hoạt động. Installer kiểm tra hash baseline trước khi ghi; nếu khác sẽ dừng.
Không bỏ kiểm tra hash để ép cài lên camera khác.

`recorder-source/20261111-195907-073264/` là mã baseline không chứa cấu hình
riêng, dùng để kiểm tra tương thích và làm fixture kiểm thử. Không chép nguyên
thư mục này lên SD: boot selector có tham chiếu chương trình làn khẩn cấp gốc
của thiết bị, chương trình đó không đi kèm gói này. Installer giữ nguyên boot
selector, module MP4, Wi-Fi và cấu hình quay hiện có trên camera.

## Chuẩn bị cấu hình trên laptop

Dùng Python 3.10 trở lên trên Linux. Công cụ USB dùng thư viện chuẩn Python;
không cần cài pyserial. Các tùy chọn chẩn đoán `curl` cần lệnh curl trên máy.

Từ thư mục gốc repo:

```bash
cd K230_CoreIoT_Recorder
cp drive-webapp.example.json drive-webapp.json
cp mqtt_config.example.json mqtt_config.json
chmod 600 drive-webapp.json mqtt_config.json
```

1. Dùng [Code.gs](../K230_AppsScript_Drive/Code.gs) và
   [appsscript.json](../K230_AppsScript_Drive/appsscript.json) cho Apps Script.
   Trong Script Properties, đặt `DEVICE_TOKEN` riêng cho upload, ít nhất 32
   ký tự; có thể đặt thêm `DRIVE_FOLDER_ID`. Triển khai Web App thực thi bằng
   tài khoản chủ Drive, cho phép thiết bị truy cập và sao chép URL `/exec`.
2. Sửa `drive-webapp.json`: thay `YOUR_DEPLOYMENT_ID` bằng deployment của bạn.
   Chạy `python3 setup-drive-token.py` và nhập DEVICE_TOKEN khi được hỏi.
   Không dùng token CoreIoT cho mục này. Với script mới, có thể dùng
   `python3 setup-drive-token.py --generate` rồi sao chép giá trị trong file
   riêng `apps-script-device-token.txt` vào Script Properties.
3. Trong CoreIoT, tạo device, lấy **access token** và điền vào
   `mqtt_access_token` trong `mqtt_config.json`; đặt `client_id` riêng cho mỗi
   camera. Device ID không thay thế access token. Cấu hình hiện tại dùng MQTT
   cổng 1883 không mã hóa; không xem đây là kết nối MQTT qua TLS.
4. Camera tiếp tục dùng cấu hình Wi-Fi có sẵn trên SD. File
   `recording_config.example.json` chỉ để tham khảo; installer không ghi đè
   cấu hình quay của camera. `auto_start: false` nghĩa là chờ KEY/CoreIoT.

## Cài và kiểm tra qua USB

Camera phải đang chờ quay. Đóng CanMV IDE để nhường `/dev/ttyACM0`. Chẩn đoán
USB tạm dừng chương trình; khởi động lại camera sau khi hoàn tất.

```bash
python3 prepare-google-pins.py
python3 install-recorder-usb.py --port /dev/ttyACM0
```

Installer kiểm tra baseline/cú pháp, sao lưu trên SD và xác minh hash sau ghi.
Sau `INSTALLED`, khởi động lại, để KEY được thả trong 5 giây đầu. Xanh dương:
sẵn sàng; đỏ: quay; vàng: đóng MP4; xanh lá: đã lưu. Cấu hình mẫu là phiên
30 giây, nhưng thời gian thực tế theo cấu hình đang lưu trên camera.

Thử quay ngắn và chờ uploader đã cài:

```bash
python3 test-recorder-usb.py --seconds 5
```

Nếu đã có clip selftest và muốn thử lại với mã HTTP hiện tại:

```bash
python3 retry-drive-usb.py --install-on-success
```

Lệnh retry lấy chứng chỉ Google đã xác minh trên laptop, thử clip cũ trong
RAM, và chỉ cài bản cập nhật khi Drive xác nhận cả MP4 lẫn JSON. Khi Google
chậm, mỗi lần đọc socket có thể chờ 60 giây. `PASS` xác nhận upload;
`INSTALLED` xác nhận cập nhật SD. Khởi động lại sau lệnh.

## Điều khiển từ CoreIoT

1. Khởi động camera, để KEY được thả trong 5 giây đầu và kiểm tra device Active.
2. CoreIoT → Dashboards → dấu `+` → Import dashboard → chọn
   `recorder-dashboard.json`. Mở **K230 Video Recorder**.
3. Nếu device không tên `K230_AI_Camera`, vào Edit → Entity aliases, sửa
   alias đó trỏ tới device của bạn, rồi lưu.
4. Bật **Bật / dừng quay** để bắt đầu; tắt để đóng/lưu clip. Hết thời gian
   phiên, camera tự dừng và công tắc cập nhật theo `session_active`.
5. Theo dõi `recorder_state`, `upload_state`, `upload_percent`, `upload_error`.
   `upload_state=uploaded` xác nhận MP4 và JSON đã được Drive nhận.
   `last_drive_url` trỏ đến video khi MP4 được xác nhận hoàn tất.

Các RPC bên dưới được xử lý bởi module `remote_control.py`:

| Method | Params | Tác dụng |
| --- | --- | --- |
| `setRecording` | `true` hoặc `false` dạng boolean | Bắt đầu/dừng phiên |
| `startRecording` | `{}` | Yêu cầu bắt đầu phiên |
| `stopRecording` | `{}` | Yêu cầu dừng và lưu |
| `getRecording` | `{}` | Trả trạng thái đang ghi |
| `getRecorderStatus` | `{}` | Trả các trạng thái recorder/upload |
| `uploadPending` | `{}` | Yêu cầu thử upload clip đang chờ |

Phản hồi `accepted` cho biết đã nhận yêu cầu; theo dõi
`recorder_state=recording` để xác nhận camera thực sự bắt đầu ghi. Lệnh quay
có thể chờ HTTP đang chạy kết thúc trước khi mở MP4.

## HTTPS và việc cập nhật chứng chỉ

Firmware đã thử không đặt được RTC (vẫn năm 1970). Bản cập nhật dùng UTC phần
mềm, lấy NTP khi khởi động độc lập, và ghim toàn bộ chứng chỉ Google do laptop
kiểm tra CA/hostname. Camera đối chiếu pin và thời hạn trước khi gửi HTTP,
token hoặc video. Đây là phương án tương thích firmware cũ, có yêu cầu bảo trì.

Google có thể đổi chứng chỉ trước ngày hết hạn. Khi báo
`https_certificate_changed_refresh_pins` hoặc `https_pin_expired_or_future`,
kết nối laptop có mạng và chạy lại `retry-drive-usb.py --install-on-success`
để xác minh và cập nhật. Không sửa mã để bỏ kiểm tra pin. Khi chưa cập nhật,
clip vẫn được giữ cục bộ. Khởi động độc lập cần truy cập NTP
`time.google.com:123/UDP`; không lấy được giờ sẽ dừng upload.

Ngày 2026-11-11, clip 5 giây cùng JSON đã upload thành công trên camera thật,
và bản sửa đã cài. Phép thử USB dùng giờ laptop. Điều khiển dashboard, khởi
động lạnh với NTP và độ ổn định dài hạn còn cần kiểm tra trên thiết bị.

## Kiểm thử và khôi phục

Kiểm thử mô phỏng không cần camera hay cấu hình riêng:

```bash
python3 run-tests.py
```

Các bài kiểm tra bao gồm RPC, đóng MP4 trước upload, TLS/pin và timeout,
HTTP redirects, resumable upload, USB output filtering, cài/rollback khi ghi
bị lỗi. Chúng không thay thế kiểm tra video/âm thanh và mạng trên K230 thật.

Khôi phục lần cài gần nhất khi camera đang chờ, IDE đã đóng:

```bash
python3 install-recorder-usb.py --port /dev/ttyACM0 --rollback
```

Sau `ROLLED BACK`, khởi động lại. Xem [RECORDER-UPDATE.md](RECORDER-UPDATE.md)
để biết chi tiết bản sao lưu, giới hạn 256 MiB/file và các bước kiểm tra.
Giữ các cấu hình thật, báo cáo, video và journal `.drive.0.json`/`.drive.1.json`
ở máy riêng; journal chứa URL upload tạm có quyền ghi.
