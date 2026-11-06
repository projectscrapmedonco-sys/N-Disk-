# Survivorship Care Workflow Assistant

An offline-first progressive web app for nurse-led cancer-survivorship tele-follow-up. The app helps nurses record or upload Tamil/English audio, review a transcript, draft important CRF fields, verify rule-based review flags, and export the completed visit proforma in the original CRF format.

## What it supports

- Separate nurse and patient portal experiences
- Breast, colorectal, gastric and ovarian cancer visit proformas
- T1-T4 visit workflows and appointment-sheet import
- Built-in audio recording and audio-file upload
- Tamil, English and mixed-language transcription adapters
- Local Whisper transcription, OpenAI transcription, or Eka Scribe integration
- Transcript-backed CRF suggestions that remain editable by the nurse
- Clinician-set rule-based flags with override reasons and audit history
- Patient blood-test report uploads
- Original-format CRF PDF export
- Encrypted browser storage with a local passphrase and automatic locking
- De-identified sharing and local audit logging

The application provides documentation support only. It does not diagnose, prescribe treatment, or replace clinical review.

## Quick start on Windows

1. Open PowerShell.
2. Change to the project folder:

   ```powershell
   cd "D:\Health-a-thon\New folder\survivorship-app-pwa"
   ```

3. Start the local app:

   ```powershell
   .\run-local.bat
   ```

4. Open the address shown by the script, normally:

   ```text
   http://127.0.0.1:8766/
   ```

Keep the server window open while using the app. Press `Ctrl+C` to stop it.

## Optional transcription providers

Copy the example environment file before configuring a provider:

```powershell
Copy-Item .env.example .env
```

### Local Whisper

Install the local dependencies and model support:

```powershell
.\setup-local-transcription.bat
```

Then start the app with `run-local.bat`. The Whisper model is cached under `.model-cache/` and is not part of the repository.

### OpenAI

Set `OPENAI_API_KEY` in `.env`, then run:

```powershell
.\run-openai.bat
```

The key is read by the local server and is never placed in browser JavaScript.

### Eka Scribe

Set the Eka configuration in `.env` and use the server-side adapter. Do not put `EKA_CLIENT_SECRET` in frontend code. Eka credentials must match the configured environment and may require an approved HTTPS origin or backend proxy.

## Project structure

```text
index.html                    Main PWA shell and nurse workflow
role-login.js                 Nurse/patient portal and login flow
crf-workflow.js               Recording, transcription and CRF workflow logic
local_transcription_server.py Local server and transcription adapters
crf-templates/                Source CRF templates
icons/                        PWA icons
vendor/                       Browser vendor assets
care-integration/             CARE/Eka Scribe integration notes and plugin source
run-local.bat                 Windows local startup script
run-openai.bat                Windows OpenAI startup script
setup-local-transcription.bat Local transcription setup script
requirements-local.txt        Python dependencies for local transcription
```

## Privacy and safety

- Use synthetic or approved test data during development.
- Do not commit `.env`, API keys, recordings, patient reports, or generated exports.
- Review consent before recording or sending audio to a cloud provider.
- A nurse must review every transcript, translation, autofill suggestion and flag before submission.
- Clinical deployment requires institutional approval, privacy review, security review and validation with clinical users.

## Current limitations

- Tamil speech accuracy depends on audio quality, speaker distance and model configuration.
- Local Whisper models can take time to download and process long recordings.
- Eka Scribe and OpenAI require valid credentials, network access and provider-specific limits.
- CARE plugin integration is prepared as an extension path, not a replacement for a validated CARE deployment.
- The prototype has not been validated for real clinical decision-making.

## License

Add the project license chosen by the team before public release.
