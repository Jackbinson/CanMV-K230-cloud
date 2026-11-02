# Gửi video K230 lên Google Drive bằng Apps Script

## Bản mẫu này làm gì

- Apps Script xác thực một mã thiết bị, tạo phiên tải lên tiếp tục được (resumable) và trả về URL tạm.
- K230 gửi các khối 256 KiB **trực tiếp tới Google Drive** qua HTTPS; Apps Script không nhận luồng MP4.
- Gửi MP4 đã hoàn tất và JSON cùng tên từ `/data/recordings` vào thư mục Drive `K230_Clips`.
- Giữ nguyên video trên thẻ. Tệp `drive_upload_state.json` chỉ ghi tên và ID Drive để tránh gửi lại tệp đã xong.

## Trạng thái kiểm tra hiện tại

- Web app Apps Script đã được triển khai. POST không token từ laptop trả HTTP 200 với `unauthorized`, đúng như thiết kế.
- K230 đã kết nối Wi-Fi và lần POST thăm dò đã kích hoạt `doPost` trong Apps Script; lần thực thi được ghi nhận là hoàn tất.
- K230 hiện vẫn nhận `HTTP bad status line: b''` khi đọc phản hồi POST. Vì vậy chưa xác nhận được ping có token hoặc tải video lên Drive.

Đây vẫn là bản thử nghiệm. Chỉ thử tải một clip nhỏ sau khi xử lý xong lỗi phản hồi HTTPS; không xem việc `doPost` chạy xong là bằng chứng video đã được tải lên.

## Tạo Apps Script

1. Mở [script.google.com](https://script.google.com/) bằng tài khoản Google của bạn, tạo dự án mới.
2. Thay nội dung `Code.gs` bằng tệp `Code.gs` trong gói này.
3. Trong Project Settings, bật **Show `appsscript.json` manifest file in editor**; thay manifest bằng `appsscript.json` trong gói. Manifest bật Drive API service cho dự án Apps Script mặc định; không cần mở Cloud Run hoặc tạo OAuth client cho K230.
4. Trong **Project Settings → Script properties**, thêm:
   - `DEVICE_TOKEN`: chuỗi bí mật ngẫu nhiên tối thiểu 32 ký tự. Có thể tạo trên Windows PowerShell bằng:

   ```powershell
   -join (1..48 | ForEach-Object { [char](Get-Random -Minimum 33 -Maximum 127) })
   ```

   Chép chuỗi đó tạm vào nơi riêng để lát nữa nhập vào tệp cấu hình board. Không gửi token qua chat.
5. Chọn **Deploy → New deployment → Web app**. Đặt **Execute as: Me**. Để board gọi được khi đang ở xa, chọn quyền truy cập **Anyone** nếu tài khoản cho phép. Google có thể yêu cầu bạn duyệt quyền Drive và URL Fetch; chấp thuận trong tài khoản của chính bạn.
6. Sao chép URL kết thúc bằng `/exec`. Không dùng URL `/dev`.

Lần đầu gọi `init`, script sẽ tạo thư mục `K230_Clips` trong Drive của tài khoản triển khai. Script dùng OAuth của chủ tài khoản; không đưa OAuth client secret hay refresh token xuống camera. Nếu tổ chức Google chặn Web App cho người dùng ẩn danh, endpoint này không thể dùng từ board cho tới khi chính sách tài khoản cho phép. Apps Script web app có thể bị giới hạn quyền `Anyone` tùy loại tài khoản/chính sách Workspace.

## Cấu hình trên thẻ (khi bạn sẵn sàng cài)

Tạo bản sao của `drive_upload_config.example.json`, đổi tên thành:

`/sdcard/recorder/drive_upload_config.json`

Điền URL `/exec`, cùng `DEVICE_TOKEN` bạn vừa tạo. `recordings_dir` hiện khớp với cấu hình recorder của dự án là `/data/recordings`. Hãy giữ bí mật tệp cấu hình/token; ai có bản sao token có thể tải tệp lên Drive của bạn. Có thể thu hồi bằng cách đổi Script Property `DEVICE_TOKEN` và sửa cấu hình board.

Chép `k230_drive_upload.py` vào board rồi chạy nó **sau khi đã dừng phiên quay**. Bản mẫu cố ý không được gắn vào vòng lặp quay: tải 30 MB qua 4G có thể mất lâu hơn khoảng nghỉ giữa hai clip và làm mất cảnh giao thông. Tệp MP4 đang ghi dở có đuôi `.partial.mp4` và sẽ bị bỏ qua.

## Giới hạn cần biết

- Apps Script/Drive không cần Cloud Run, K230BurningTool, Rufus hay Zadig. Không cần bật Cloud Run API hoặc trả khoản đặt cọc Cloud Run.
- Drive cá nhân thường có 15 GB dùng chung với Gmail và Photos. Nhu cầu video đã ước tính trước đây khoảng 18–24 GB; vì vậy Drive miễn phí không chứa được toàn bộ bộ dữ liệu nếu không tải về/xóa bản Drive cũ hoặc nâng dung lượng. Bản này **không tự xóa** tệp trên thẻ hoặc Drive.
- Tải video qua kết nối 4G vẫn dùng dung lượng data của SIM/router. Nên dùng Wi-Fi có Internet để thử clip nhỏ trước.
- Tệp lớn được gửi trực tiếp tới Drive theo phiên resumable. Nếu board khởi động lại giữa một tệp, bản mẫu hiện chưa lưu URL phiên tải lên để tiếp tục qua lần khởi động; có thể cần chạy lại và phát sinh bản trùng nếu Drive đã nhận tệp nhưng board chưa ghi trạng thái.
- Không tích hợp vào `main.py`, không ghi lên SD, không xóa video, không thay model hoặc firmware trong bước chuẩn bị này.

## Thư mục gói

- `Code.gs` — endpoint Apps Script.
- `appsscript.json` — quyền OAuth tối thiểu cần dùng.
- `k230_drive_upload.py` — uploader CanMV MicroPython.
- `drive_upload_config.example.json` — cấu hình mẫu, không chứa thông tin thật.
