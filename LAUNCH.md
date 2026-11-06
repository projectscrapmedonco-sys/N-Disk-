# Launching the Survivorship app

## A. On one laptop (fastest, data stays on that laptop)
1. Unzip this folder. Needs Python 3.
2. For Tamil audio transcription on Windows, first double-click `setup-local-transcription.bat`. The Whisper model downloads on first use into this folder's `.model-cache` on D: (the default is the faster `base` model; set `WHISPER_MODEL=small` when accuracy is more important than speed).
3. Mac/Linux: run `./run-local.sh`  ·  Windows: double-click `run-local.bat`.
4. Open **http://127.0.0.1:8766** in **Chrome or Edge**.

If the page says that the transcription service is unavailable, close old
tabs, keep the launcher open, and reopen **http://127.0.0.1:8766/index.html?build=53**.
The hosted preview and the in-app browser do not provide the local `/api/transcribe`
service needed for audio translation.

## OpenAI transcription option

For higher-quality Tamil transcription, create a new API key in the OpenAI dashboard (never
paste it into this chat or `index.html`), open PowerShell in this folder, and run:

```powershell
$env:OPENAI_API_KEY = "your-new-key"
.\run-openai.bat
```

This uses `gpt-transcribe` for the Tamil source and a separate text pass for the English draft.
The recording leaves the laptop, and the nurse must still correct and verify the CRF before
submission. `run-local.bat` automatically uses OpenAI when `OPENAI_API_KEY` is set; without a
key it remains the privacy-preserving offline fallback.
4. Install it: Chrome/Edge address bar → install icon (or ⋮ → *Install Survivorship*). It then opens in its own window and works offline.
5. First open: create a passphrase (8+ characters). Forgotten passphrase = data cannot be recovered.

## B. For your team (a website everyone can open)
Put this folder on any **HTTPS** static host: your institution's web server (nginx/Apache), GitHub Pages, Netlify or Cloudflare Pages. No server code is needed.
- Phones: Android Chrome → ⋮ → *Install app*. iPhone Safari → Share → *Add to Home Screen*.
- **Data is stored separately in each browser on each device** (and separately for each web address). Nurses will not see each other's data until a backend is added.
- The nurse/patient selector in this prototype separates the browser experience only. Nurse data remains protected by the local passphrase; patient access uses a participant ID only for demonstration and must not be used for real patient data.
- Tamil dictation is retained as the source transcript. The app creates an offline English clinical draft from the symptom vocabulary used in its CRF, then requires a nurse to review/correct the English text and explicitly verify and submit the CRF. This is not a certified medical translation; use an approved clinically validated translation workflow before handling real patient data.
- To release an update: edit `index.html`, change `VERSION` in `sw.js`, redeploy. Users get it the next time they reopen the app.

## C. Real multi-user system
Use an authenticated CARE plugin/bridge or equivalent backend behind HTTPS with real logins. This static folder does not include a production backend; one is needed before real patient data.

## D. Prepare a local Open Healthcare Network CARE workspace
This folder now contains a safe preparation package in `care-integration/`. Start with `care-integration/README.md`, then run `powershell -ExecutionPolicy Bypass -File .\care-integration\care-preflight.ps1`. It only checks prerequisites and makes no changes.

The supplied CARE EkaScribe frontend plugin is integrated at
`care-integration\care_eka_scribe_fe`. Read `care-integration\CARE_SCRIBE_INTEGRATION.md`
and run `care-integration\run-care-scribe.bat` after setting its Eka token in the
plugin's `.env`. This is a CARE Module Federation remote; it is not a direct
script dependency of the static PWA.

Use the official CARE Scaffold in a **separate workspace** for a local, synthetic-data plugin environment. The static PWA must stay behind a same-origin authenticated bridge; `POST /api/transcribe` is a bridge contract, not a public CARE endpoint. Do not upload patient recordings to public/demo CARE servers or place provider keys in the browser.

## Audio transcription integration

The nurse tele-visit page accepts WAV, MP3, M4A, OGG and WebM recordings up to 50 MB. Tamil is selected by default for this workflow. After recording consent is ticked, choose **Upload audio** to send the file to the same-origin local service; the English draft is shown for nurse correction before CRF autofill.

The Docker-free local launcher now provides a same-origin `POST /api/transcribe` endpoint. It runs faster-whisper locally, returns the source transcript plus an English translation, and deletes the temporary audio file after processing. Tamil is translated in short independent speech windows so a long recording cannot carry a repeated decoder state into later answers. Browser uploads are queued as background jobs and polled, so a long recording does not lose its result when one HTTP request times out. Repetitive or low-quality output is flagged and is not autofilled into the CRF. The first transcription downloads the selected Whisper model into `.model-cache` on D:. The default decoder uses a laptop-friendly beam size; set `WHISPER_BEAM_SIZE=5` for slower, higher-quality decoding, or set `WHISPER_MODEL=small` after downloading the complete small model. For real patient data, use your organisation's approved and authenticated deployment instead.

The endpoint receives multipart fields:

- audio — the recording file
- language — ta-IN, en-IN, or auto

Return JSON with required transcript, optional language, and optional english_transcript fields. For example: {"transcript":"Tamil or English source transcript","language":"ta-IN","english_transcript":"Optional English draft"}.

The nurse must review the English text and the CRF before submission. The endpoint must enforce nurse authentication and visit authorization, validate file type/size, use an approved transcription service, and follow the organisation's retention and deletion rules. Do not put a cloud transcription API key in this static app.

### Original-format CRF download

After the nurse verifies the visit, **Download filled CRF** calls the local Python service and returns a two-page PDF containing only the matching original Breast, Colorectal, or Gastric visit proforma. It preserves the original visit-page layout (Breast pages 19–20; Colorectal/Gastric pages 16–17) and omits all unrelated CRF pages. The export is available when the app is running with `.venv\Scripts\python.exe` (run `setup-local-transcription.bat` once first). The browser-only fallback can display the PWA but cannot create the original-format PDF because it has no `/api/download-crf` endpoint.

### Eka Scribe provider

To use the supplied Eka session flow for CRF drafting, copy `.env.example` to `.env` and set either `EKA_ACCESS_TOKEN` or `EKA_CLIENT_ID` plus `EKA_CLIENT_SECRET`. Leave `TRANSCRIPTION_PROVIDER` blank for automatic selection, or set it to `eka` to force Eka. The local backend then performs authentication, creates the Eka session, uploads the raw audio, ends the session, polls for completion, and passes `transcript` plus `structured_data` into the nurse CRF workflow. The browser still calls only the same-origin `/api/transcribe` endpoint; client credentials never enter browser JavaScript. Eka single-upload sessions are limited to 10 MB; use a shorter recording or add the documented chunked flow for longer audio.

## Notes
- Mic dictation works best in Chrome/Edge; it sends audio to the browser vendor's speech service. Not for real patient calls without approval.
- Prototype: use synthetic data until clinical, security and data-protection review is done.
- Not tested here: actual installation on a phone or in a browser (this was checked only by loading every file from a local server).

## If the page does not open
1. Keep the black server window open. It must say "Serving HTTP on 127.0.0.1 port 8766".
2. Type the address exactly as **http://127.0.0.1:8766** (http, not https; 127.0.0.1, not localhost).
3. Port in use? Edit the port in the launcher and use that same number in the address.
4. Windows: `run-local.bat` uses the built-in PowerShell server (`serve.ps1`) — **no Python needed** (the "Python was not found… Microsoft Store" message is a Windows shortcut and is not used). If your PC blocks PowerShell scripts, install Python from python.org and run `python -m http.server 8766` inside the folder. If Windows shows "Windows protected your PC", choose *More info → Run anyway*. Or skip the server: double-click `index.html` (works in Chrome/Edge, but cannot be installed or used offline).
5. Do not run it from inside the zip: unzip first.
