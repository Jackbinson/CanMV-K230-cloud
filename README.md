# CanMV K230: quay video, CoreIoT và Google Drive

Quay bằng KEY hoặc điều khiển từ CoreIoT; tự upload MP4 và JSON metadata lên
Google Drive khi camera chờ và có Wi-Fi. Camera gửi video trực tiếp đến Drive,
dùng Google Apps Script để tạo phiên resumable upload; không cần VPS.

Bắt đầu với **[K230_CoreIoT_Recorder/README.md](K230_CoreIoT_Recorder/README.md)**.
Thư mục này chứa bản cập nhật recorder, công cụ cài/khôi phục qua CanMV USBDBG,
dashboard CoreIoT, cấu hình mẫu và các kiểm thử chạy trên laptop.

## Đã kiểm tra trên thiết bị

Ngày 2026-11-11, Yahboom K230 (CanMV USBDBG 0.4.0, MicroPython 1.21.0) đã quay
clip thử 5 giây, upload MP4 cùng JSON lên Google Drive và cài bản sửa lên SD
thành công. Wi-Fi và kết nối MQTT cũng đã được xác nhận trên camera.

Firmware này không đặt được đồng hồ hệ thống. Bản cập nhật dùng UTC phần mềm
và chứng chỉ Google ghim do laptop kiểm tra CA/hostname trước khi cài. Khi
Google thay chứng chỉ cần cập nhật pin. Phép thử USB dùng giờ laptop; lấy NTP
sau khởi động và thao tác dashboard vẫn cần kiểm tra trên camera thực tế.

## Các công cụ trước đây

- [K230_AppsScript_Drive](K230_AppsScript_Drive/HUONG_DAN.md): Apps Script và
  các công cụ chẩn đoán/upload độc lập trước bản tích hợp recorder.
- [cloudflare_worker](cloudflare_worker/SETUP_VI.md): phương án relay trước
  đây; bản recorder mới dùng kết nối trực tiếp và không yêu cầu Worker.

## Cấu hình riêng

Chỉ cấu hình mẫu được đưa lên repo. Giữ token CoreIoT, token upload Drive,
mật khẩu Wi-Fi, OAuth credentials, báo cáo thiết bị và journal upload ở máy
riêng. Không đưa các file cấu hình đang dùng trên camera lên GitHub.
