# CanMV K230 cloud upload

This repository contains a Google Apps Script endpoint and CanMV MicroPython files for uploading K230 recordings to Google Drive.

## Current verification status

As of 2026-11-02, Apps Script reports completed `doGet` executions from the K230, but CanMV receives no HTTP status bytes from the Apps Script web-app route. The direct Apps Script path is therefore not usable on this board yet. A Cloudflare Worker relay has been prepared to follow Google's response redirects and return the final response to the K230. The relay and video upload have **not** been verified on the physical board.

Start with [`K230_AppsScript_Drive/HUONG_DAN.md`](K230_AppsScript_Drive/HUONG_DAN.md) and [`K230_AppsScript_Drive/HUONG_DAN_CHAY_PING_CANMV.md`](K230_AppsScript_Drive/HUONG_DAN_CHAY_PING_CANMV.md).

To try the relay, see [`cloudflare_worker/SETUP_VI.md`](cloudflare_worker/SETUP_VI.md).

## Secrets

Do not commit the real `drive_upload_config.json`, Wi-Fi configuration, device token, or OAuth credentials. Use `drive_upload_config.example.json` as a template and keep device-specific files local.
