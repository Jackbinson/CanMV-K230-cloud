# K230 recorder: CoreIoT và Google Drive

Bản này lấy từ chương trình quay trên Yahboom K230 (CanMV USBDBG 0.4.0,
MicroPython 1.21.0). Ngày 2026-11-11, phép thử trên camera thật đã quay clip
5 giây, upload MP4 cùng JSON lên Google Drive và cài bản sửa lên SD thành công.
Báo cáo thử và thông tin tài khoản được giữ riêng trên máy, không đưa lên repo.
Điều khiển từ dashboard và lấy NTP sau khi khởi động lại vẫn cần kiểm tra trên
thiết bị; phép thử upload dùng giờ UTC phần mềm do laptop cung cấp qua USB.

Đọc [README.md](README.md) để tạo cấu hình riêng trước khi chạy các lệnh dưới.
Đây là bản cập nhật cho đúng baseline Yahboom đi kèm, không phải firmware đầy đủ.

## Cài qua USB

Để camera chờ quay, đóng CanMV IDE, chạy trong Terminal:

```bash
cd K230_CoreIoT_Recorder
python3 prepare-google-pins.py
python3 install-recorder-usb.py --port /dev/ttyACM0
```

Lệnh kiểm tra mã gốc, cú pháp MicroPython và khả năng IPv4/TLS; sao lưu trên
SD; chép cấu hình CoreIoT/Drive từ các file riêng trên laptop; kiểm tra SHA-256
sau khi ghi. Không in token. Thư mục sao lưu được in khi thành công.

Sau dòng `INSTALLED`, khởi động lại K230, không bấm KEY trong 5 giây đầu để
vào recorder. LED xanh dương là sẵn sàng; đỏ là quay, vàng là đóng MP4, xanh lá
là đã lưu phiên. Cấu hình hiện tại: 640×480, clip 30 giây, phiên 30 giây,
`/data/recordings`. Bấm KEY vẫn quay/dừng được.

Lệnh chỉ kiểm tra tương thích, không ghi file lên camera:

```bash
python3 install-recorder-usb.py --port /dev/ttyACM0 --preflight
```

## Dashboard CoreIoT

Trong CoreIoT → Dashboards → Import dashboard, chọn file:

`recorder-dashboard.json`

Alias tìm device tên `K230_AI_Camera`. Nếu tên device khác, sửa alias trỏ vào
đúng device đã tạo của bạn. File không chứa device UUID cũ hay access token.
Công tắc gọi `setRecording` với boolean; trạng thái công tắc lấy từ telemetry
`session_active` nên tự tắt sau phiên quay. Các thẻ hiển thị trạng thái quay,
upload, clip, phần trăm, lỗi gần nhất và đường dẫn Drive.

RPC hỗ trợ: `setRecording(true/false)`, `startRecording`, `stopRecording`,
`getRecording`, `getRecorderStatus`, `uploadPending`. Không cần rule chain
báo động của chương trình làn khẩn cấp. Telemetry được gửi khoảng 5 giây/lần.
Lệnh bắt đầu được chấp nhận khác với đã bắt đầu ghi: theo dõi
`recorder_state=recording` và `recording=true`.

## Thử trên camera

Có thể thử bằng Terminal trước khi dùng dashboard. Khởi động lại K230, đợi
đèn xanh dương READY, đóng CanMV IDE rồi chạy:

```bash
cd K230_CoreIoT_Recorder
python3 test-recorder-usb.py --seconds 5
```

Lệnh tạm chạy một phiên thử: quay 5 giây, đóng/kiểm tra MP4 rồi chờ chính
uploader chạy nền đã cài gửi MP4 và JSON. Clip mang tên `TEST_...`, lưu trong
một thư mục `selftest-...` mới, được giữ lại cả cục bộ và trên Drive. Cấu hình
quay đang lưu không bị đổi. Mặc định chờ upload tối đa 180 giây; Terminal hiện
tiến độ và lỗi đã che thông tin nhạy cảm. `PASS` xác nhận MP4 cùng metadata đã
được Drive nhận; vẫn mở `drive_url` để kiểm tra hình/âm thanh. Sau khi thử,
khởi động lại K230 để trở về chương trình quay thường. Báo cáo trên laptop:
`recorder-test-result.json`. Trong phiên thử, các RPC yêu cầu quay/dừng khác bị
từ chối với `usb_test_in_progress` để giữ phiên thử riêng biệt.

Nếu đã có clip selftest nhưng upload báo `http_connection_failed`, có thể thử
lại clip đó mà không quay mới:

```bash
cd K230_CoreIoT_Recorder
python3 retry-drive-usb.py --install-on-success
```

Để phiên thử cũ kết thúc, khởi động lại K230 rồi đóng IDE trước khi chạy. Lệnh
tải các chứng chỉ Google đã xác minh CA/hostname từ laptop, tạm dùng bản sửa
HTTP/1.1 trong RAM với giờ UTC phần mềm, rồi thử lại clip finalized trong selftest.
Không cần đặt RTC của firmware. Chỉ sau khi Drive xác nhận
cả MP4 và JSON, tùy chọn `--install-on-success` mới cài bản sửa lên SD, có sao
lưu. Bỏ tùy chọn này nếu chỉ muốn thử trong RAM. Journal được tiếp tục nếu có;
không xóa file và không in token/URL upload tạm. Báo cáo: `drive-retry-result.json`.
Chứng chỉ được lưu trong `google-pins.json` trên laptop và SD. Sau `PASS` và
`INSTALLED`, khởi động lại K230 để dùng bản sửa trong chương trình
thường. Nếu thử không thành công thì mã đã lưu trên SD vẫn là bản trước đó.

1. Sau khởi động, CoreIoT device phải Active và Latest telemetry phải có
   `recorder_state`, `session_active`, `upload_state`.
2. Bật công tắc dashboard, kiểm tra camera LED đỏ và telemetry recording.
3. Tắt công tắc sau khoảng 5–10 giây. Camera đóng và kiểm tra MP4 như chương
   trình cũ; `last_stop_reason=remote_stop`, `last_video` là file `.mp4`.
4. Lúc camera chờ, upload chạy. Theo dõi `upload_bytes`, `upload_total_bytes`,
   `upload_percent`, `upload_state`, `upload_error`.
5. Mở video từ `last_drive_url`, phát thử có hình/âm thanh và kiểm tra duration.
   `upload_state=uploaded` xác nhận cả MP4 và JSON metadata đã được Drive nhận.
   URL video chỉ được công bố sau khi Drive xác nhận MP4 hoàn tất.
6. Thử mất Wi-Fi rồi quay bằng KEY: video vẫn lưu cục bộ, upload thử lại khi
   kết nối trở lại. Thử stop/start thêm một lần sau khi đã upload.

Apps Script dùng `DRIVE_FOLDER_ID` nếu có; nếu chưa đặt sẽ tạo `K230_Clips`.
Uploader giữ toàn bộ video cục bộ; tự gửi cả backlog clip đã finalized của
camera_id hiện tại. Không gửi `.partial.mp4`, metadata lỗi/needs_review.

## Tác động và giới hạn cần kiểm tra thực tế

Không thay `main.py`, `clean_mp4.py`, `mp4_metrics.py`, `wifi_client.py`,
`recording_config.json`, ROI, Wi-Fi hay video hiện có. Chỉ thay `record_video.py`,
thêm module mạng/CA (`remote_control`, `mqtt_transport`, `drive_upload`,
`recorder_https`, `google_ca`) cùng cấu hình CoreIoT, Drive và chứng chỉ ghim.
Không thay firmware/MediaManager.

MQTT và Drive chạy hai luồng riêng. Uploader chỉ khởi tạo HTTP lúc recorder
đang chờ; lệnh quay mới chờ HTTP hiện tại kết thúc rồi mới mở MP4. Client mới
gửi HTTP/1.1 bằng socket IPv4, ghi đủ dữ liệu khi socket chỉ nhận một phần,
đọc status/header từng byte và hỗ trợ chunked; không dùng bộ đọc `requests`
của firmware. Nó theo redirect trong danh sách host Google, đổi POST sang GET
ở 302/303, giữ 308 PUT là phản hồi tiến độ Drive.

Firmware đã trả `errno=1` khi đặt RTC và vẫn đọc năm 1970. Phương án được chọn
là giữ firmware và cho K230 tự upload khi laptop tắt, dùng chứng chỉ ghim.
Laptop lấy chứng chỉ từ Google qua TLS có kiểm tra CA và hostname; lưu SHA-256
toàn bộ chứng chỉ cùng thời hạn và IP đã xác minh. Camera mở TLS rồi đối chiếu
chính xác chứng chỉ nhận được và kiểm tra thời hạn bằng UTC phần mềm, trước
khi gửi bất kỳ HTTP header, token hoặc video nào. Chứng chỉ không khớp, hết
hạn, thiếu API đọc peer certificate hay thiếu giờ phần mềm đều làm upload dừng.
Native TLS dùng CERT_NONE trong riêng nhánh pin vì RTC lỗi; xác thực peer được
thực hiện bởi bước kiểm tra pin sau handshake. Không có nhánh chấp nhận mọi
chứng chỉ rồi gửi token. Nhánh CA chuẩn vẫn dùng CERT_REQUIRED cho firmware hỗ trợ.

Google có thể thay chứng chỉ mà không báo trước. Nếu nhận
`https_certificate_changed_refresh_pins` hoặc `https_pin_expired_or_future`,
chạy lại `retry-drive-usb.py --install-on-success` trên laptop có mạng để lấy
pin mới và cập nhật SD. Đây là chi phí bảo trì của phương án tương thích
firmware cũ; không bảo đảm pin giữ nguyên đến ngày hết hạn. Trong lúc chờ cập
nhật, video cục bộ được giữ và không bị đánh dấu upload thành công.

Kết nối socket/TLS đặt timeout 15 giây; sau handshake, mỗi lần đọc/ghi socket
có timeout 60 giây. Khi chờ Google có thể không có log mới trong khoảng này.
Không hạ timeout xuống 2 giây vì firmware có thể coi timeout là EOF của TLS.
Nếu luồng bị đóng hoặc timeout, GET được thử lại một lần từ URL ban đầu để
lấy redirect mới; không tự gửi lại POST/PUT có kết quả chưa rõ. SSL sở hữu
socket gốc và chỉ được đóng một lần, tránh đóng nhầm descriptor đã tái sử dụng.
DNS của firmware vẫn có thể chậm hơn giới hạn socket.
Client lấy NTP từ `time.google.com:123/UDP`, lưu UTC cùng mốc
`ticks_ms()` và làm mới sau một giờ; không cần sửa RTC. Mỗi lần khởi động cần
lấy được giờ NTP trước khi upload. Khi chạy qua USB, giờ laptop được dùng làm
mốc UTC tin cậy cho phiên thử. `rtc_year=1970` vẫn có thể xuất hiện; xem
`verification_year`/`upload_verification_year` để biết năm dùng xác minh.
Nếu không lấy được giờ phần mềm sẽ báo `https_software_time_unavailable`.
Google Web App đã phản hồi không ổn định trong phép thử laptop; token khớp
không khẳng định đường upload đã ổn định. Lỗi mạng chỉ dẫn tới retry; không
đánh dấu upload hoàn tất khi chưa có xác nhận HTTP 200/201 cùng file ID.

Mỗi file giới hạn 256 MiB theo Code.gs hiện có. Gửi theo khối 256 KiB, lưu URL
phiên và tiến độ trong hai slot `.drive.0.json`/`.drive.1.json` cạnh clip.
Không chia sẻ các file journal này: chúng chứa URL upload tạm thời.
Lúc retry, hỏi tiến độ Drive trước khi gửi tiếp, kể cả khi mất phản hồi của
khối cuối. Nếu phiên Drive đã hết hạn thì mở phiên mới; trường hợp phiên đã
hoàn tất nhưng kết quả bị mất đến khi phiên hết hạn có thể tạo bản trùng.
Video cục bộ và metadata gốc được giữ nguyên để đối chiếu.

Các bài kiểm tra host dùng filesystem/socket/encoder giả lập; chưa thay thế
việc kiểm tra khung hình, tài nguyên media, TLS và upload trên K230 thật.

## Khôi phục

Để camera chờ, đóng IDE, chạy:

```bash
cd K230_CoreIoT_Recorder
python3 install-recorder-usb.py --port /dev/ttyACM0 --rollback
```

Khởi động lại sau khi báo `ROLLED BACK`. Lệnh khôi phục đúng mã/cấu hình đã sao
lưu trước lần cài gần nhất; từ chối ghi đè file đã sửa sau khi cài. Nếu cài bị
ngắt, `maintenance.flag` ngăn boot recorder vào bộ file chưa hoàn tất. Chạy
`--rollback` để khôi phục; nếu không thể xác minh backup, gửi thông báo lỗi để
kiểm tra, giữ nguyên backup và cờ bảo trì.
