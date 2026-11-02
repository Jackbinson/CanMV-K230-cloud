# CanMV K230 cloud upload

This repository contains a Google Apps Script endpoint and CanMV MicroPython files for uploading K230 recordings to Google Drive.

## Current verification status

As of 2026-11-02, a token-free POST from the laptop reaches the Apps Script web app and returns `200` with `unauthorized`, as expected. The K230 connects to Wi-Fi and its POST triggers a completed `doPost` execution, but CanMV receives no HTTP response bytes for that POST. A GET-polling workaround has been added to `Code.gs` and the CanMV scripts; it requires replacing `Code.gs` and deploying a new web-app version. The workaround and video upload have **not** been verified on the physical board.

Start with [`K230_AppsScript_Drive/HUONG_DAN.md`](K230_AppsScript_Drive/HUONG_DAN.md) and [`K230_AppsScript_Drive/HUONG_DAN_CHAY_PING_CANMV.md`](K230_AppsScript_Drive/HUONG_DAN_CHAY_PING_CANMV.md).

## Secrets

Do not commit the real `drive_upload_config.json`, Wi-Fi configuration, device token, or OAuth credentials. Use `drive_upload_config.example.json` as a template and keep device-specific files local.
