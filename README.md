# 🎵 b33t — Freedom to Music

<p align="center">
  <img src="static/icon.png" width="128" height="128" alt="b33t logo" style="border-radius: 28px; box-shadow: 0 10px 30px rgba(0,0,0,0.6);" />
</p>

<p align="center">
  <strong>Ultra-Fast, Spotify-Grade Music Streaming for Your Laptop & Desktop.</strong><br>
  <em>Zero Subscriptions. Zero Ads. Zero Telemetry. 100% Freedom.</em>
</p>

<p align="center">
  <a href="#-quickstart"><img src="https://img.shields.io/badge/Status-Active%20%26%20Stable-1d9bf0?style=for-the-badge" alt="Status"></a>
  <a href="#-features"><img src="https://img.shields.io/badge/Audio-Native%20Opus%20251-2ecc71?style=for-the-badge" alt="Audio"></a>
  <a href="#-architecture"><img src="https://img.shields.io/badge/Privacy-Zero%20Telemetry-ff3b5c?style=for-the-badge" alt="Privacy"></a>
  <a href="#-license"><img src="https://img.shields.io/badge/License-MIT-f39c12?style=for-the-badge" alt="License"></a>
</p>

---

## ✊ The Manifesto: Freedom to Music

Streaming platforms have turned music into a walled garden of monthly paywalls, intrusive audio ads, algorithmic lock-in, and aggressive behavioral tracking.

**b33t** breaks that mold:
* **No Subscriptions**: Complete access to the world's music without a credit card or monthly fee.
* **No Audio Ads**: Pure, uninterrupted playback.
* **No Forced Accounts**: No emails, passwords, phone numbers, or account lockouts.
* **Private by Default**: Runs locally on your machine (`127.0.0.1`). What you listen to remains your business.
* **High-Fidelity Audio**: Streams native, uncompressed **YouTube Opus 251** audio directly without lossy FFmpeg re-encoding.

---

## ✨ Features

- 🎧 **Obsidian-Dark Spotify Aesthetic**: Precision-engineered dark UI with high contrast, responsive grid layouts, custom scrollbars, and seamless transitions.
- ⚡ **Sub-Second Playback Engine**: Prewarms audio streams and leverages asynchronous worker queues for near-instant playback response.
- 🌍 **Instant Global & African Discographies**: Curated artist discographies (Bien, Sauti Sol, Burna Boy, Drake, Kendrick Lamar, Ayra Starr, Travis Scott, The Weeknd, and more).
- 🔎 **DeepSeek Music Search**: Real-time auto-suggest query engine pulling official music tracks, albums, and remixes.
- 💾 **Intelligent Local Cache**: Automatically persists streamed tracks to local disk (`audio_cache/*.opus`) for offline replay without re-downloading.
- ⌨️ **Desktop Keyboard Controls**: Seamless playback controls tailored for laptops and workstations.

---

## 🚀 Quickstart

### Method 1: Direct Python (Recommended)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ngesa-cloud/b33t-desktop.git
   cd b33t-desktop
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Launch the player**:
   ```bash
   ./run.sh
   # Or directly: python3 server.py
   ```

4. Open your browser at **[http://localhost:8080](http://localhost:8080)**. That's it!

---

### Method 2: Docker Compose (1-Command Anywhere)

Run **b33t** in an isolated container without installing Python or dependencies on your host:

```bash
docker compose up -d
```
Access the player at `http://localhost:8080`.

---

## 🎹 Desktop Keyboard Shortcuts

| Key | Action |
| :---: | :--- |
| `Space` | Toggle Play / Pause |
| `→` (Right Arrow) | Seek forward 5 seconds |
| `←` (Left Arrow) | Seek backward 5 seconds |
| `Shift + →` | Skip to next track in queue |
| `Shift + ←` | Previous track |
| `↑` / `↓` | Volume Up / Volume Down |
| `M` | Mute / Unmute Audio |
| `Esc` | Close drawers, modals, and fullscreen lyrics |

---

## 🏛️ System Architecture

```
                    ┌──────────────────────────────────┐
                    │  Desktop Browser / Web App       │
                    │  (Obsidian-Dark High-Speed UI)   │
                    └─────────────────┬────────────────┘
                                      │ HTTP / REST
                                      ▼
                    ┌──────────────────────────────────┐
                    │    b33t Python Server (:8080)    │
                    │    • Flask Threaded Runtime      │
                    │    • Stream Dispatcher           │
                    │    • Prewarm ThreadPool (4 workers)│
                    └───────┬──────────────────┬───────┘
                            │                  │
               Fetch / Stream│                  │ Disk Cache Lookup
                            ▼                  ▼
              ┌──────────────────┐    ┌──────────────────┐
              │ ytmusicapi &     │    │  audio_cache/    │
              │ yt-dlp Extractor │    │  (*.opus files)  │
              │ (Format 251 Opus)│    └──────────────────┘
              └──────────────────┘
```

---

## 📡 REST API Reference

For developers and tinkerers building custom frontends or CLI players:

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/api/ping` | `GET` | Health status and node hostname |
| `/api/version` | `GET` | Active version and streaming engine status |
| `/api/search?q=<query>` | `GET` | Search for songs and official videos |
| `/api/stream/<video_id>` | `GET` | Direct binary audio stream (supports Range requests) |
| `/api/featured_artists` | `GET` | Curated artists catalog and metadata |
| `/api/artist/<name>/tracks` | `GET` | Top discography tracks for artist |
| `/api/status/<video_id>` | `GET` | Query download & cache state |

---

## 🔒 Security & Privacy Notice

**b33t** is built on privacy-first engineering:
* Does not collect telemetry, analytics, IP logs, or personal identifiers.
* All audio caching happens exclusively on your local filesystem under `audio_cache/`.
* No account credentials or passwords are ever required.

---

## 📜 License

Released under the **MIT License**. Crafted with passion by [Dominic Nyongesa](https://github.com/ngesa-cloud).
