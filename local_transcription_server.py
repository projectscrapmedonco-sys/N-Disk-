"""Docker-free local server for the survivorship PWA.

Serves the existing static app and exposes POST /api/transcribe on the same
origin.  The browser never receives a provider key and the uploaded recording
is deleted as soon as both Whisper passes finish.  This is intended for a
single Windows laptop and synthetic/de-identified testing, not production
clinical deployment.
"""

from __future__ import annotations

import json
import io
import os
import re
import tempfile
import threading
import time
import uuid
import mimetypes
import urllib.error
import urllib.request
from collections import Counter
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse


ROOT = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE settings without overwriting process variables."""
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Accept the common shortened typo while keeping the canonical
            # setting name used by the server and the documentation.
            if key == "OPEN_API_KEY":
                key = "OPENAI_API_KEY"
            if key:
                # A locally edited .env should replace a stale inherited
                # secret from an earlier launcher process. Non-secret settings
                # retain normal environment-variable precedence.
                if key in {"OPENAI_API_KEY", "EKA_ACCESS_TOKEN", "EKA_CLIENT_ID", "EKA_CLIENT_SECRET"}:
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)
    except OSError:
        pass


_load_env_file(ROOT / ".env")
HOST = os.environ.get("SURVIVORSHIP_HOST", "127.0.0.1")
PORT = int(os.environ.get("SURVIVORSHIP_PORT", "8766"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# The small model is noticeably better for Tamil.  The launcher falls back to
# base if the small model cannot be loaded (for example while its first
# download is still incomplete), so an offline demo still starts.
MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
MODEL_FALLBACK = os.environ.get("WHISPER_FALLBACK_MODEL", "base")
# A wider beam and a small repetition penalty reduce Tamil decoder loops.
BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
TRANSLATION_WINDOW_SECONDS = float(os.environ.get("WHISPER_TRANSLATION_WINDOW", "24"))
TRANSLATION_GAP_SECONDS = float(os.environ.get("WHISPER_TRANSLATION_GAP", "1.5"))
TRANSCRIPTION_PROVIDER = os.environ.get("TRANSCRIPTION_PROVIDER", "").strip().lower()
if not TRANSCRIPTION_PROVIDER:
    if os.environ.get("EKA_ACCESS_TOKEN", "").strip() or (
        os.environ.get("EKA_CLIENT_ID", "").strip()
        and os.environ.get("EKA_CLIENT_SECRET", "").strip()
    ):
        TRANSCRIPTION_PROVIDER = "eka"
    elif os.environ.get("OPENAI_API_KEY", "").strip():
        TRANSCRIPTION_PROVIDER = "openai"
    else:
        TRANSCRIPTION_PROVIDER = "local"
OPENAI_TRANSCRIPTION_MODEL = os.environ.get("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
OPENAI_TRANSLATION_MODEL = os.environ.get("OPENAI_TRANSLATION_MODEL", "gpt-4.1-mini")
OPENAI_FLAG_MODEL = os.environ.get("OPENAI_FLAG_MODEL", "gpt-4.1-mini")
EKA_BASE_URL = os.environ.get("EKA_BASE_URL", "https://api.eka.care").rstrip("/")
EKA_MODEL = os.environ.get("EKA_MODEL", "pro").strip() or "pro"
EKA_TEMPLATE_IDS = [
    item.strip()
    for item in os.environ.get("EKA_TEMPLATE_IDS", "clinical_notes_template").split(",")
    if item.strip()
][:2]
EKA_POLL_SECONDS = float(os.environ.get("EKA_POLL_SECONDS", "1"))
EKA_POLL_TIMEOUT_SECONDS = int(os.environ.get("EKA_POLL_TIMEOUT_SECONDS", "300"))
CRF_TEMPLATES = {
    "B": {"file": "breast-crf.pdf", "visit_start": 18, "name": "Breast"},
    "C": {"file": "colorectal-crf.pdf", "visit_start": 15, "name": "Colorectal"},
    "G": {"file": "gastric-crf.pdf", "visit_start": 15, "name": "Gastric"},
}
TAMIL_INITIAL_PROMPT = (
    "இது ஒரு தமிழ் மருத்துவ பின்தொடர்பு உரையாடல். "
    "பேசப்பட்டதை மட்டும் துல்லியமாக எழுதுங்கள்; கேட்காத சொற்களை உருவாக்க வேண்டாம். "
    "முக்கிய சொற்கள்: வலி, வீக்கம், கட்டி, தோல் மாற்றம், காய்ச்சல், குமட்டல், "
    "வாந்தி, சோர்வு, உணர்வின்மை, குடல், இரத்தப்போக்கு, பசி, எடை, மூச்சுத்திணறல், "
    "மருந்து, சிகிச்சை, பின்தொடர்பு."
)
ENGLISH_INITIAL_PROMPT = (
    "Clinical follow-up conversation. Preserve symptoms, medicines, numbers, "
    "and yes/no answers exactly; do not invent a diagnosis."
)
MODEL_LOCK = threading.Lock()
WHISPER_MODEL = None
LOADED_MODEL_NAME = None
JOBS = {}
JOB_LOCK = threading.Lock()
JOB_TTL_SECONDS = 60 * 60


def json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def is_repetitive(text: str) -> bool:
    words = re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)
    if len(words) < 12:
        return False
    counts = Counter(words)
    most_common = counts.most_common(1)[0][1]
    sentences = [s.strip() for s in re.split(r"[.!?]+", text.lower()) if s.strip()]
    repeated_sentence = len(sentences) >= 4 and Counter(sentences).most_common(1)[0][1] >= 3
    return repeated_sentence or most_common / len(words) >= 0.55 or (len(counts) <= 3 and len(words) >= 8)


def collapse_repetition(text: str) -> tuple[str, bool]:
    """Collapse obvious decoder loops while preserving one meaningful utterance."""
    changed = False
    text, numeric_replacements = re.subn(r"\b(\d+)(?:[-\s]+\1){3,}\b", r"\1 [repeated number]", text)
    changed = numeric_replacements > 0
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentence_out = []
    for sentence in sentences:
        normalized = re.sub(r"[^\w\u0b80-\u0bff]+", " ", sentence.lower(), flags=re.UNICODE).strip()
        previous = re.sub(r"[^\w\u0b80-\u0bff]+", " ", sentence_out[-1].lower(), flags=re.UNICODE).strip() if sentence_out else ""
        if normalized and normalized == previous:
            changed = True
            continue
        sentence_out.append(sentence)
    # Decoder loops often repeat a question-and-answer block, e.g. two
    # sentences repeated dozens of times with tiny punctuation changes.
    def norm(value: str) -> str:
        return re.sub(r"[^\w\u0b80-\u0bff]+", " ", value.lower(), flags=re.UNICODE).strip()
    for block_size in range(min(6, len(sentence_out) // 3), 0, -1):
        i = 0
        while i + block_size * 3 <= len(sentence_out):
            block = [norm(x) for x in sentence_out[i:i + block_size]]
            reps = 1
            while i + (reps + 1) * block_size <= len(sentence_out) and [norm(x) for x in sentence_out[i + reps * block_size:i + (reps + 1) * block_size]] == block:
                reps += 1
            if reps >= 2:
                sentence_out[i:i + reps * block_size] = sentence_out[i:i + block_size]
                changed = True
                i += block_size
            else:
                i += 1
    text = " ".join(sentence_out)
    words = text.split()
    for size in range(min(8, len(words) // 3), 0, -1):
        i = 0
        while i + size * 3 <= len(words):
            gram = words[i:i + size]
            reps = 1
            while i + (reps + 1) * size <= len(words) and words[i + reps * size:i + (reps + 1) * size] == gram:
                reps += 1
            if reps >= 3:
                words[i:i + reps * size] = gram
                changed = True
                i += size
            else:
                i += 1
    cleaned = " ".join(words).strip()
    return cleaned, changed or cleaned != text.strip()


def load_whisper():
    global WHISPER_MODEL, LOADED_MODEL_NAME
    if WHISPER_MODEL is not None:
        return WHISPER_MODEL
    with MODEL_LOCK:
        if WHISPER_MODEL is not None:
            return WHISPER_MODEL
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "Local transcription is not installed. Run setup-local-transcription.bat first."
            ) from exc
        kwargs = {
            "device": os.environ.get("WHISPER_DEVICE", "cpu"),
            "compute_type": os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
        }
        try:
            WHISPER_MODEL = WhisperModel(MODEL_NAME, **kwargs)
            LOADED_MODEL_NAME = MODEL_NAME
        except Exception:
            if MODEL_NAME == MODEL_FALLBACK:
                raise
            # A partially downloaded small model should not make the offline
            # workflow unusable.  Base is slower to understand Tamil, but is
            # already cached by the setup script.
            WHISPER_MODEL = WhisperModel(MODEL_FALLBACK, **kwargs)
            LOADED_MODEL_NAME = MODEL_FALLBACK
        return WHISPER_MODEL


def parse_upload(handler: SimpleHTTPRequestHandler) -> tuple[bytes, str, str]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise ValueError("Send multipart form data containing an audio file.")
    if content_length > MAX_UPLOAD_BYTES:
        raise ValueError("Audio exceeds the 50 MB limit.")
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("The audio request must use multipart/form-data.")
    body = handler.rfile.read(content_length)
    envelope = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=default).parsebytes(envelope)
    language = "ta-IN"
    filename = "recording.m4a"
    audio = None
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if field_name == "language":
            language = payload.decode("utf-8", "replace").strip() or language
        elif field_name == "audio":
            filename = part.get_filename() or filename
            audio = payload
    if not audio:
        raise ValueError("No audio file was provided.")
    return audio, language, Path(filename).suffix.lower() or ".m4a"


def _translation_windows(segments, duration: float):
    """Group speech segments into short independent translation windows.

    Translating a nineteen-minute recording in one decoder context is prone to
    repeating a question or a number. Short windows retain the source timing
    while preventing a bad decoder state from poisoning the rest of the file.
    """
    windows = []
    current = []
    current_start = current_end = None
    for segment in segments:
        text = (getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        start = max(0.0, float(getattr(segment, "start", 0.0) or 0.0))
        end = min(duration, float(getattr(segment, "end", start) or start))
        if end <= start:
            continue
        if current and (
            start - current_end > TRANSLATION_GAP_SECONDS
            or end - current_start > TRANSLATION_WINDOW_SECONDS
        ):
            windows.append((current_start, current_end, current))
            current = []
            current_start = current_end = None
        if not current:
            current_start = start
        current.append(segment)
        current_end = end
    if current:
        windows.append((current_start, current_end, current))
    return windows


def _translate_windows(model, audio, segments, detected_language: str, progress) -> str:
    """Translate each short speech window independently with Whisper."""
    import numpy as np

    duration = len(audio) / 16000.0
    windows = _translation_windows(segments, duration)
    translated = []
    for index, (start, end, _window_segments) in enumerate(windows, 1):
        progress(f"translating to English ({index}/{len(windows)})")
        # Keep a little context around the words but never feed long silence to
        # the decoder. This also prevents padding from being mistaken for speech.
        lo = max(0, int((start - 0.25) * 16000))
        hi = min(len(audio), int((end + 0.25) * 16000))
        chunk = np.asarray(audio[lo:hi], dtype=np.float32)
        if chunk.size < 1600:
            continue
        english_segments, _ = model.transcribe(
            chunk,
            language=detected_language,
            task="translate",
            vad_filter=False,
            beam_size=max(BEAM_SIZE, 3),
            best_of=max(BEAM_SIZE, 3),
            temperature=(0.0, 0.2, 0.4),
            repetition_penalty=1.05,
            no_repeat_ngram_size=3,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            initial_prompt=ENGLISH_INITIAL_PROMPT,
            hotwords="pain swelling lump fever nausea vomiting fatigue numbness bleeding appetite weight breathlessness medicine treatment follow-up",
            without_timestamps=True,
        )
        part = " ".join(segment.text.strip() for segment in english_segments).strip()
        if part:
            translated.append(part)
    return " ".join(translated).strip()


def _local_transcribe_and_translate(audio_path: str, requested_language: str, progress=None) -> dict:
    progress = progress or (lambda _stage: None)
    progress("loading model")
    model = load_whisper()
    # faster-whisper 1.2.x passes metadata_errors to PyAV. Newer PyAV wheels
    # removed that keyword, so decode here and pass a 16 kHz mono array.
    import av
    import numpy as np

    progress("decoding audio")
    chunks = []
    with av.open(audio_path) as container:
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=16000
        )
        for frame in container.decode(audio=0):
            converted = resampler.resample(frame)
            if not isinstance(converted, list):
                converted = [converted]
            chunks.extend(item.to_ndarray() for item in converted)
        flushed = resampler.resample(None)
        if not isinstance(flushed, list):
            flushed = [flushed]
        chunks.extend(item.to_ndarray() for item in flushed)
    if not chunks:
        raise RuntimeError("The audio file contains no decodable samples.")
    audio = np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0
    language = requested_language.split("-")[0].lower()
    forced_language = None if language in {"", "auto"} else language
    progress("transcribing source")
    source_segments, source_info = model.transcribe(
        audio,
        language=forced_language,
        task="transcribe",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 650, "speech_pad_ms": 300},
        beam_size=max(BEAM_SIZE, 5),
        best_of=max(BEAM_SIZE, 5),
        temperature=(0.0, 0.2, 0.4),
        repetition_penalty=1.05,
        no_repeat_ngram_size=3,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        hallucination_silence_threshold=1.5,
        condition_on_previous_text=True,
        prompt_reset_on_temperature=0.5,
        initial_prompt=TAMIL_INITIAL_PROMPT if forced_language in {None, "ta"} else ENGLISH_INITIAL_PROMPT,
        hotwords="வலி வீக்கம் கட்டி காய்ச்சல் குமட்டல் வாந்தி சோர்வு உணர்வின்மை இரத்தப்போக்கு பசி எடை மூச்சுத்திணறல் மருந்து சிகிச்சை பின்தொடர்பு",
        without_timestamps=False,
    )
    source_segment_list = list(source_segments)
    raw_source_text = " ".join(segment.text.strip() for segment in source_segment_list).strip()
    source_text, source_collapsed = collapse_repetition(raw_source_text)
    confidence_values = [
        float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        for segment in source_segment_list
        if getattr(segment, "text", "") and getattr(segment, "avg_logprob", None) is not None
    ]
    low_confidence = bool(confidence_values) and (
        sum(score < -1.0 for score in confidence_values) / len(confidence_values) >= 0.45
    )
    detected_language = getattr(source_info, "language", None) or forced_language or "ta"
    if detected_language == "en":
        raw_english_text = source_text
    else:
        progress("translating to English")
        raw_english_text = _translate_windows(
            model, audio, source_segment_list, detected_language, progress
        )
    english_text, english_collapsed = collapse_repetition(raw_english_text)
    if not source_text or not english_text:
        raise RuntimeError("The local model returned an empty transcript.")
    result = {
        "transcript": source_text,
        "english_transcript": english_text,
        "language": f"{detected_language}-IN" if detected_language == "ta" else detected_language,
        "model": LOADED_MODEL_NAME or MODEL_NAME,
        "translation_mode": "segmented-independent-windows",
        "requires_nurse_review": True,
    }
    if low_confidence or source_collapsed or english_collapsed or is_repetitive(raw_source_text) or is_repetitive(raw_english_text):
        result["quality_warning"] = (
            "The transcript has low confidence or repetitive text, so the CRF was not autofilled. "
            "Re-record closer to the speaker or upload a clearer audio file, then have the nurse review it."
        )
    return result


def _eka_request(
    method: str,
    url: str,
    token: str = "",
    payload: dict | None = None,
    raw: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = 1800,
) -> tuple[int, dict]:
    data = raw
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    parsed = {"raw": body}
                return response.status, parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            if exc.code == HTTPStatus.ACCEPTED:
                return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}
            if exc.code in {502, 503, 504} and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if exc.code == HTTPStatus.GATEWAY_TIMEOUT:
                detail = (
                    "Eka's gateway timed out while processing this request. Verify that EKA_BASE_URL "
                    "matches the credential environment, then retry with a shorter audio file."
                )
            else:
                detail = parsed.get("message") or parsed.get("detail") or parsed.get("error") or body
            raise RuntimeError(f"Eka request failed (HTTP {exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Eka network request failed: {exc.reason}") from exc


def _eka_access_token() -> str:
    token = os.environ.get("EKA_ACCESS_TOKEN", "").strip()
    if token:
        return token
    client_id = os.environ.get("EKA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EKA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "TRANSCRIPTION_PROVIDER=eka requires EKA_ACCESS_TOKEN or EKA_CLIENT_ID and "
            "EKA_CLIENT_SECRET in the local .env file."
        )
    _status, body = _eka_request(
        "POST",
        f"{EKA_BASE_URL}/connect-auth/v1/account/login",
        payload={"client_id": client_id, "client_secret": client_secret},
    )
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Eka authentication returned no access_token.")
    return access_token


def _eka_structured_data(body: dict) -> dict:
    structured: dict = {}
    templates = body.get("templates") or []
    if isinstance(templates, dict):
        templates = [templates]
    for template in templates:
        if not isinstance(template, dict):
            continue
        for template_id, result in template.items():
            if not isinstance(result, dict) or result.get("status") not in {"success", "completed"}:
                continue
            data = result.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    structured[template_id] = data
                    continue
            if isinstance(data, dict):
                structured.update(data)
            elif data not in (None, ""):
                structured[template_id] = data
    return structured


def _eka_transcribe_and_translate(audio_path: str, requested_language: str, progress=None) -> dict:
    progress = progress or (lambda _stage: None)
    audio = Path(audio_path).read_bytes()
    if len(audio) > 10 * 1024 * 1024:
        raise RuntimeError("Eka single-upload sessions accept audio up to 10 MB. Use a shorter recording or the chunked Eka flow.")
    token = _eka_access_token()
    language = requested_language.split("-")[0].lower()
    language_hint = [language] if language and language != "auto" else ["ta", "en"]
    progress("creating Eka session")
    _status, session = _eka_request(
        "POST",
        f"{EKA_BASE_URL}/voice/v1/sessions",
        token=token,
        payload={
            "upload_type": "single",
            "model": EKA_MODEL,
            "language_hint": language_hint,
            "templates": EKA_TEMPLATE_IDS or ["clinical_notes_template"],
        },
    )
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("Eka session creation returned no session_id.")
    filename = Path(audio_path).name or "consultation.webm"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    progress("uploading audio to Eka")
    _eka_request(
        "POST",
        f"{EKA_BASE_URL}/voice/v1/sessions/{quote(session_id, safe='')}/audio/{quote(filename, safe='')}",
        token=token,
        raw=audio,
        content_type=content_type,
    )
    progress("starting Eka processing")
    _eka_request(
        "POST",
        f"{EKA_BASE_URL}/voice/v1/sessions/{quote(session_id, safe='')}/end",
        token=token,
        payload=None,
    )
    progress("waiting for Eka transcript and structured note")
    deadline = time.time() + EKA_POLL_TIMEOUT_SECONDS
    result_body: dict = {}
    while time.time() < deadline:
        status, result_body = _eka_request(
            "GET",
            f"{EKA_BASE_URL}/voice/v1/sessions/{quote(session_id, safe='')}",
            token=token,
        )
        if status in {HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT}:
            break
        if status != HTTPStatus.ACCEPTED:
            raise RuntimeError(f"Eka returned an unexpected session status: {status}.")
        time.sleep(max(0.2, EKA_POLL_SECONDS))
    else:
        raise RuntimeError("Eka processing timed out before returning a transcript.")
    transcript = str(result_body.get("transcript") or result_body.get("text") or "").strip()
    structured_data = _eka_structured_data(result_body)
    if not transcript and not structured_data:
        raise RuntimeError("Eka returned no transcript or structured clinical note.")
    return {
        "transcript": transcript,
        "english_transcript": "",
        "structured_data": structured_data,
        "language": f"{language}-IN" if language and language != "auto" else "auto",
        "model": EKA_MODEL,
        "provider": "eka",
        "session_id": session_id,
        "requires_nurse_review": True,
        "review_note": "Eka generated this transcript and structured draft; the nurse must edit, verify, and submit the CRF.",
    }


def _crf_text(value) -> str:
    return "" if value is None else str(value).strip()


def _crf_draw_text(canvas, page_height: float, x: float, top: float, value, max_chars: int = 48, size: float = 8.0):
    text = _crf_text(value)
    if not text:
        return
    canvas.setFont("Helvetica", size)
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    for index, line in enumerate(lines[:3]):
        canvas.drawString(x, page_height - top - index * (size + 2), line[:max_chars])


def _crf_draw_value(canvas, page_height: float, x: float, top: float, label: str, value, max_chars: int = 42):
    _crf_draw_text(canvas, page_height, x, top, value, max_chars=max_chars, size=8.0)


def _crf_draw_single_line(canvas, page_height: float, x: float, top: float, value, max_chars: int = 32, size: float = 7.5):
    """Draw a row-level value without wrapping into the next printed row."""
    text = _crf_text(value)
    # Older browser drafts stored a verbose label in every advice cell. Strip
    # that presentation prefix during export so saved visits remain readable.
    text = re.sub(r"^general advice from transcript:\s*", "", text, flags=re.IGNORECASE)
    if not text:
        return
    if len(text) > max_chars:
        text = text[: max(1, max_chars - 1)].rstrip() + "…"
    canvas.setFont("Helvetica", size)
    canvas.drawString(x, page_height - top, text)


def _crf_overlay_page(canvas, page_height: float, page_number: int, payload: dict, cancer: str):
    patient = payload.get("patient") or {}
    visit = payload.get("visit") or {}
    complaints = visit.get("complaints") or []
    recurrence = visit.get("recurrence") or []
    side_effects = visit.get("side_effects") or []
    is_breast = cancer == "B"
    is_gastric = cancer == "G"
    first_page = page_number == (19 if is_breast else 16)
    second_page = page_number == (20 if is_breast else 17)
    if first_page:
        if is_breast:
            tag = _crf_text(visit.get("tag")).upper()
            if tag in {"T1", "T2", "T3", "T4"}:
                canvas.setFont("Helvetica-Bold", 8)
                # Match the printed checkbox baseline in the visit header.
                canvas.drawString(311 + (int(tag[1]) - 1) * 26, page_height - 84, "X")
            # Breast template has a wider left label column than the other
            # visit templates. Keep values inside the right-hand cells.
            _crf_draw_value(canvas, page_height, 236, 101, "patient", patient.get("name"))
            _crf_draw_value(canvas, page_height, 236, 134, "id", patient.get("id"))
            _crf_draw_value(canvas, page_height, 236, 194, "date", visit.get("date"))
            _crf_draw_value(canvas, page_height, 390, 194, "time", visit.get("time"))
            _crf_draw_value(canvas, page_height, 236, 221, "mode", visit.get("mode"))
            _crf_draw_value(canvas, page_height, 365, 262, "provider", visit.get("provider"))
        elif is_gastric:
            _crf_draw_value(canvas, page_height, 235, 82, "patient", patient.get("name"))
            _crf_draw_value(canvas, page_height, 235, 125, "date", visit.get("date"))
            _crf_draw_value(canvas, page_height, 235, 147, "time", visit.get("time"))
            _crf_draw_value(canvas, page_height, 235, 168, "mode", visit.get("mode"))
            _crf_draw_value(canvas, page_height, 235, 204, "provider", visit.get("provider"))
        else:
            _crf_draw_value(canvas, page_height, 205, 96, "patient", patient.get("name"))
            _crf_draw_value(canvas, page_height, 205, 119, "caregiver", visit.get("caregiver"))
            _crf_draw_value(canvas, page_height, 205, 143, "date", visit.get("date"))
            _crf_draw_value(canvas, page_height, 205, 166, "time", visit.get("time"))
            _crf_draw_value(canvas, page_height, 205, 189, "mode", visit.get("mode"))
            _crf_draw_value(canvas, page_height, 205, 219, "provider", visit.get("provider"))
        # The breast template's complaint row baseline sits lower than the
        # other templates; use the row baseline (not the top border) so the
        # entered text lines up with the printed 1/2/3 markers.
        complaint_top = 376 if is_breast else (269 if is_gastric else 289)
        complaint_step = 23 if not is_gastric else 16
        complaint_x = 255 if is_breast else (240 if is_gastric else 225)
        for index, complaint in enumerate(complaints[:3 if is_breast else 5]):
            _crf_draw_single_line(canvas, page_height, complaint_x, complaint_top + index * complaint_step, complaint, max_chars=55, size=7.5)
        row_tops = [498, 524, 550, 576, 602] if is_breast else ([392, 414, 435, 457, 479, 500, 522] if is_gastric else [461, 484, 507, 530, 553, 576, 599, 622, 645])
        severity_x = 330 if is_breast else (350 if is_gastric else 168)
        advice_x = 435 if is_breast else (448 if is_gastric else 295)
        for index, item in enumerate(recurrence[:len(row_tops)]):
            _crf_draw_single_line(canvas, page_height, severity_x, row_tops[index] + 8, item.get("severity"), max_chars=18, size=7.5)
            _crf_draw_single_line(canvas, page_height, advice_x, row_tops[index] + 8, item.get("advice"), max_chars=24, size=7.2)
        if is_breast:
            for index, item in enumerate(side_effects[:3]):
                top = [678, 704, 731][index]
                _crf_draw_single_line(canvas, page_height, 330, top + 8, item.get("severity"), max_chars=18, size=7.5)
                _crf_draw_single_line(canvas, page_height, 435, top + 8, item.get("advice"), max_chars=24, size=7.2)
        elif is_gastric:
            for index, item in enumerate(side_effects[:5]):
                top = [628, 650, 672, 693, 715][index]
                _crf_draw_single_line(canvas, page_height, 350, top + 8, item.get("severity"), max_chars=18, size=7.5)
                _crf_draw_single_line(canvas, page_height, 448, top + 8, item.get("advice"), max_chars=24, size=7.2)
    if second_page:
        row_tops = [35, 61, 88, 115, 141, 168, 194, 221, 247, 275, 300, 328, 355, 382, 409, 436] if is_breast else ([] if is_gastric else [88, 111, 134, 157, 180, 203, 226, 249, 272])
        side_effect_items = side_effects[3:] if is_breast else ([] if is_gastric else side_effects)
        for index, item in enumerate(side_effect_items[:len(row_tops)]):
            _crf_draw_single_line(canvas, page_height, 330 if is_breast else 168, row_tops[index] + 8, item.get("severity"), max_chars=18, size=7.5)
            _crf_draw_single_line(canvas, page_height, 435 if is_breast else 295, row_tops[index] + 8, item.get("advice"), max_chars=24, size=7.2)
        if visit.get("other_symptoms"):
            other_x = 220 if is_breast else 180
            other_top = 480 if is_breast else (52 if is_gastric else 320)
            _crf_draw_text(canvas, page_height, other_x, other_top, "Other symptoms: " + _crf_text(visit.get("other_symptoms")), max_chars=86, size=7.5)
        ecog = _crf_text(visit.get("ecog"))
        if ecog.isdigit():
            breast_ecog_tops = [527, 555, 597, 640, 665, 691]
            colorectal_ecog_tops = [386, 407, 445, 483, 508, 532]
            gastric_ecog_tops = [97, 119, 151, 183, 200, 217]
            tops = breast_ecog_tops if is_breast else (gastric_ecog_tops if is_gastric else colorectal_ecog_tops)
            index = min(max(int(ecog), 0), len(tops) - 1)
            # drawString uses a text baseline.  The printed checkbox is
            # centred a few points below the row's top coordinate.
            top = tops[index] + 8
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawString(42 if is_breast else 28, page_height - top, "X")


def build_filled_crf_pdf(payload: dict) -> tuple[bytes, str]:
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("PDF export dependencies are missing. Run setup-local-transcription.bat again.") from exc
    cancer = str(payload.get("cancer") or "").upper()
    info = CRF_TEMPLATES.get(cancer)
    if not info:
        raise ValueError("No original CRF template is configured for this cancer group.")
    template = ROOT / "crf-templates" / info["file"]
    if not template.exists():
        raise RuntimeError(f"Original CRF template is missing: {info['file']}")
    reader = PdfReader(str(template))
    writer = PdfWriter()
    start = int(info["visit_start"])
    # Export only the current visit proforma pages. The unrelated screening,
    # treatment, follow-up, and appendix pages are intentionally omitted.
    for index in (start, start + 1):
        page = reader.pages[index]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        buffer = io.BytesIO()
        overlay = canvas.Canvas(buffer, pagesize=(width, height))
        _crf_overlay_page(overlay, height, index + 1, payload, cancer)
        # ReportLab can emit a zero-page PDF when the visit is still blank.
        # Always finalize one overlay page so an empty/incomplete CRF can
        # still be downloaded without a pypdf "Sequence index out of range".
        overlay.showPage()
        overlay.save()
        buffer.seek(0)
        overlay_reader = PdfReader(buffer)
        if not overlay_reader.pages:
            raise RuntimeError("Could not create the CRF overlay page.")
        page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    patient_id = _crf_text((payload.get("patient") or {}).get("id")) or "patient"
    return output.getvalue(), f"{patient_id}-{info['name'].lower()}-visit-crf.pdf"


def _openai_json(path: str, payload: dict, api_key: str, timeout: int = 1800) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.openai.com/v1/{path.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"OpenAI request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI network request failed: {exc.reason}") from exc


def _openai_multipart(audio_path: str, requested_language: str, api_key: str) -> dict:
    audio = Path(audio_path).read_bytes()
    if len(audio) > 25 * 1024 * 1024:
        raise RuntimeError("OpenAI file transcription accepts audio up to 25 MB.")
    boundary = f"----survivorship-{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
    parts = []

    def field(name: str, value: str) -> None:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    field("model", OPENAI_TRANSCRIPTION_MODEL)
    field("response_format", "json")
    field("chunking_strategy", "auto")
    field("temperature", "0")
    language = requested_language.split("-")[0].lower()
    if language and language != "auto":
        field("language", language)
    field(
        "prompt",
        "இது ஒரு தமிழ் மருத்துவ பின்தொடர்பு உரையாடல். பேச்சாளர்களின் சொற்களைத் துல்லியமாக "
        "எழுதுங்கள்; கேட்காத சொற்களை உருவாக்க வேண்டாம். முக்கியமான சொற்கள்: வலி, வீக்கம், "
        "கட்டி, தோல் மாற்றம், காய்ச்சல், குமட்டல், வாந்தி, சோர்வு, உணர்வின்மை, குடல், "
        "இரத்தப்போக்கு, பசி, எடை, மூச்சுத்திணறல், மருந்து, மற்றும் பின்தொடர்பு.",
    )
    filename = Path(audio_path).name
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        audio,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"OpenAI transcription failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI transcription network request failed: {exc.reason}") from exc


def _response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    pieces = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                pieces.append(content["text"])
    return "\n".join(pieces).strip()


AI_FLAG_INSTRUCTIONS = (
    "You are an AI clinical documentation safety reviewer for a survivorship follow-up form. "
    "Review only the supplied transcript and CRF values. Identify statements or values that "
    "need a nurse's attention before submission. Do not diagnose, triage, prescribe, or invent "
    "facts. Do not convert a question into a symptom. A flag is a review priority, not medical "
    "advice. Preserve uncertainty and mention when the evidence is unclear. Return ONLY valid JSON "
    "with this shape: {\"summary\":\"short summary\",\"issues\":[{"
    "\"issue\":\"short label\",\"urgency\":\"routine|priority|urgent\","
    "\"confidence\":0.0,\"evidence\":\"short quote or CRF field\","
    "\"reason\":\"why the nurse should review it\","
    "\"fields\":[\"CRF field names\"],\"requires_nurse_review\":true}]}"
)


def _clean_ai_flag_json(text: str) -> dict:
    """Parse and constrain the model response before it reaches the browser."""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The AI flagging model returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("The AI flagging model returned an invalid result.")
    issues = []
    for raw in value.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        issue = str(raw.get("issue") or "").strip()
        if not issue:
            continue
        urgency = str(raw.get("urgency") or "routine").lower().strip()
        if urgency not in {"routine", "priority", "urgent"}:
            urgency = "routine"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
        issues.append(
            {
                "issue": issue[:240],
                "urgency": urgency,
                "confidence": round(confidence, 2),
                "evidence": str(raw.get("evidence") or "").strip()[:500],
                "reason": str(raw.get("reason") or "").strip()[:500],
                "fields": [str(field).strip()[:120] for field in fields[:8] if str(field).strip()],
                "requires_nurse_review": True,
            }
        )
    return {
        "summary": str(value.get("summary") or "No additional AI flags were identified.").strip()[:600],
        "issues": issues[:20],
    }


def _openai_flag_issues(payload: dict) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AI flagging requires OPENAI_API_KEY in the server environment.")
    safe_payload = {
        "cancer": str(payload.get("cancer") or "").strip(),
        "transcript": str(payload.get("transcript") or "").strip()[:6000],
        "english_transcript": str(payload.get("english_transcript") or "").strip()[:6000],
        "complaints": payload.get("complaints") or [],
        "recurrence": payload.get("recurrence") or [],
        "side_effects": payload.get("side_effects") or [],
        "ecog": payload.get("ecog") or "",
        "other_symptoms": str(payload.get("other_symptoms") or "").strip()[:1000],
        "medications": str(payload.get("medications") or "").strip()[:2000],
        "rule_flags": payload.get("rule_flags") or [],
    }
    response = _openai_json(
        "responses",
        {
            "model": OPENAI_FLAG_MODEL,
            "store": False,
            "instructions": AI_FLAG_INSTRUCTIONS,
            "input": json.dumps(safe_payload, ensure_ascii=False),
        },
        api_key,
        timeout=180,
    )
    result = _clean_ai_flag_json(_response_text(response))
    result.update(
        {
            "model": OPENAI_FLAG_MODEL,
            "provider": "openai",
            "requires_nurse_review": True,
            "review_note": "AI flags are review prompts only; the nurse must verify the transcript and CRF.",
        }
    )
    return result


def _openai_transcribe_and_translate(audio_path: str, requested_language: str, progress=None) -> dict:
    progress = progress or (lambda _stage: None)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TRANSCRIPTION_PROVIDER=openai requires OPENAI_API_KEY in the launcher environment.")
    progress("uploading audio to OpenAI")
    transcription = _openai_multipart(audio_path, requested_language, api_key)
    source_text = str(transcription.get("text", "")).strip()
    if not source_text:
        raise RuntimeError("OpenAI returned an empty transcript.")
    progress("translating Tamil to English")
    translation = _openai_json(
        "responses",
        {
            "model": OPENAI_TRANSLATION_MODEL,
            "store": False,
            "instructions": (
                "Translate the Tamil transcript into accurate, natural English for a nurse's "
                "review. Preserve uncertainty with [unclear]. Do not diagnose, add facts, or "
                "turn questions into answers. Keep symptom names, medicines, numbers, and yes/no "
                "answers faithful. Return only the English translation."
            ),
            "input": source_text,
        },
        api_key,
    )
    english_text = _response_text(translation)
    if not english_text:
        raise RuntimeError("OpenAI returned an empty English translation.")
    result = {
        "transcript": source_text,
        "english_transcript": english_text,
        "language": "ta-IN",
        "model": f"{OPENAI_TRANSCRIPTION_MODEL}+{OPENAI_TRANSLATION_MODEL}",
        "provider": "openai",
        "translation_mode": "openai-file-transcription+reviewed-translation",
        "requires_nurse_review": True,
        "review_note": "OpenAI generated this draft; the nurse must edit, verify, and submit the CRF.",
    }
    if is_repetitive(source_text) or is_repetitive(english_text):
        result["quality_warning"] = (
            "The OpenAI output is repetitive, so the CRF was not autofilled. "
            "Re-record closer to the speaker or have the nurse translate the unclear section."
        )
    return result


def transcribe_and_translate(audio_path: str, requested_language: str, progress=None) -> dict:
    if TRANSCRIPTION_PROVIDER == "eka":
        return _eka_transcribe_and_translate(audio_path, requested_language, progress)
    if TRANSCRIPTION_PROVIDER == "openai":
        return _openai_transcribe_and_translate(audio_path, requested_language, progress)
    return _local_transcribe_and_translate(audio_path, requested_language, progress)


def _prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for key, job in list(JOBS.items()):
        if job.get("updated", 0) < cutoff:
            JOBS.pop(key, None)


def _update_job(job_id: str, **fields) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated"] = time.time()


def _run_job(job_id: str, temporary_path: str, language: str) -> None:
    _update_job(job_id, status="running", stage="loading model")
    try:
        result = transcribe_and_translate(
            temporary_path,
            language,
            progress=lambda stage: _update_job(job_id, stage=stage),
        )
        _update_job(job_id, status="complete", stage="complete", result=result)
    except Exception as exc:
        print(f"Transcription job {job_id} failed: {type(exc).__name__}: {exc}", flush=True)
        detail = str(exc).strip() or "Transcription failed. Check the server window."
        _update_job(job_id, status="error", stage="failed", detail=detail[:500])
    finally:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status: int, value: dict) -> None:
        data = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            # The browser may close a long request after switching tabs; the
            # background job must continue without producing a server traceback.
            pass

    def send_bytes(self, status: int, data: bytes, content_type: str, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def end_headers(self) -> None:
        # During local development, always fetch the current HTML/service worker
        # instead of retaining an old shell in the browser HTTP cache.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/transcribe/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOB_LOCK:
                _prune_jobs()
                job = dict(JOBS.get(job_id) or {})
            if not job:
                self.send_json(HTTPStatus.NOT_FOUND, {"detail": "Transcription job not found."})
                return
            payload = {
                "job_id": job_id,
                "status": job.get("status", "queued"),
                "stage": job.get("stage", "queued"),
            }
            if job.get("status") == "complete":
                payload.update(job.get("result") or {})
            elif job.get("status") == "error":
                payload["detail"] = job.get("detail", "Local transcription failed.")
            self.send_json(HTTPStatus.OK, payload)
            return
        if path == "/health":
            try:
                import faster_whisper  # noqa: F401
                installed = True
            except ImportError:
                installed = False
            provider = TRANSCRIPTION_PROVIDER if TRANSCRIPTION_PROVIDER in {"eka", "openai"} else "local"
            model = (
                EKA_MODEL
                if provider == "eka"
                else f"{OPENAI_TRANSCRIPTION_MODEL}+{OPENAI_TRANSLATION_MODEL}"
                if provider == "openai"
                else MODEL_NAME
            )
            key_configured = (
                bool(os.environ.get("EKA_ACCESS_TOKEN", "").strip())
                or bool(os.environ.get("EKA_CLIENT_ID", "").strip() and os.environ.get("EKA_CLIENT_SECRET", "").strip())
                if provider == "eka"
                else bool(os.environ.get("OPENAI_API_KEY", "").strip())
                if provider == "openai"
                else True
            )
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "provider": provider,
                    "model": model,
                    "key_configured": key_configured,
                    "ai_flag_model": OPENAI_FLAG_MODEL,
                    "ai_flag_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                    "transcriber": "faster-whisper" if installed else None,
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/flag-issues":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 2 * 1024 * 1024:
                    raise ValueError("Invalid AI flagging request.")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("AI flagging request must be a JSON object.")
                result = _openai_flag_issues(payload)
                self.send_json(HTTPStatus.OK, result)
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            except RuntimeError as exc:
                status = (
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if "requires OPENAI_API_KEY" in str(exc)
                    else HTTPStatus.BAD_GATEWAY
                )
                self.send_json(status, {"detail": str(exc)})
            except Exception as exc:
                print(f"AI flagging failed: {type(exc).__name__}: {exc}", flush=True)
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})
            return
        if path == "/api/download-crf":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 2 * 1024 * 1024:
                    raise ValueError("Invalid CRF export request.")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                pdf, filename = build_filled_crf_pdf(payload)
                self.send_bytes(HTTPStatus.OK, pdf, "application/pdf", filename)
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            except Exception as exc:
                print(f"CRF PDF export failed: {type(exc).__name__}: {exc}", flush=True)
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})
            return
        if path != "/api/transcribe":
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
            return
        temporary_path = None
        async_job = self.headers.get("X-Transcription-Async") == "1"
        try:
            audio, language, suffix = parse_upload(self)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
                temporary.write(audio)
                temporary_path = temporary.name
            if async_job:
                job_id = uuid.uuid4().hex
                with JOB_LOCK:
                    _prune_jobs()
                    JOBS[job_id] = {"status": "queued", "stage": "queued", "created": time.time(), "updated": time.time()}
                worker = threading.Thread(target=_run_job, args=(job_id, temporary_path, language), daemon=True)
                worker.start()
                temporary_path = None  # the worker owns cleanup
                self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id, "status": "queued", "stage": "queued"})
            else:
                result = transcribe_and_translate(temporary_path, language)
                self.send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
        except RuntimeError as exc:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"detail": str(exc)})
        except Exception as exc:  # keep provider/model details out of the browser
            print(f"Transcription failed: {type(exc).__name__}: {exc}")
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"detail": "Local transcription failed. Check the server window."},
            )
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def log_message(self, format: str, *args) -> None:
        print(format % args)


if __name__ == "__main__":
    print(f"Serving Survivorship PWA + local transcription on http://{HOST}:{PORT}")
    if TRANSCRIPTION_PROVIDER == "eka":
        print(f"Transcription provider: Eka Scribe ({EKA_BASE_URL}; {EKA_MODEL})")
    elif TRANSCRIPTION_PROVIDER == "openai":
        print(f"Transcription provider: OpenAI ({OPENAI_TRANSCRIPTION_MODEL} + {OPENAI_TRANSLATION_MODEL})")
    else:
        print(f"Transcription provider: local Whisper ({MODEL_NAME})")
    print(f"Whisper model: {MODEL_NAME} (downloaded on first transcription)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
