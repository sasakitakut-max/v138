from __future__ import annotations

import json
import asyncio
import base64
import io
import os
import re
import tempfile
import time
import wave

import numpy as np
import streamlit as st
import streamlit.components.v1 as components

from app_state_v132_from_uploaded import reset_webrtc_turn_state

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except Exception:
    EDGE_TTS_AVAILABLE = False

try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except Exception:
    SR_AVAILABLE = False

try:
    from streamlit_webrtc import webrtc_streamer, WebRtcMode
    WEBRTC_AVAILABLE = True
except Exception:
    webrtc_streamer = None
    WebRtcMode = None
    WEBRTC_AVAILABLE = False


@st.cache_data(show_spinner=False)
def synthesize_tts(text, voice="ja-JP-NanamiNeural", rate="+15%"):
    if not EDGE_TTS_AVAILABLE:
        return None
    text = (text or '').strip()
    if not text:
        return None

    async def _speak():
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
            await communicate.save(tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            return data
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    try:
        return asyncio.run(_speak())
    except RuntimeError:
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_speak())
            finally:
                loop.close()
        except Exception:
            return None
    except Exception:
        return None


def play_audio_immediately(audio_bytes: bytes):
    if not audio_bytes:
        return
    st.session_state.audio_render_nonce += 1
    nonce = st.session_state.audio_render_nonce
    b64 = base64.b64encode(audio_bytes).decode()
    components.html(
        f"""
        <audio id="audio-{nonce}" autoplay controls style="width:100%;">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        <script>
        const audio = document.getElementById("audio-{nonce}");
        if (audio) {{
            const p = audio.play();
            if (p !== undefined) {{
                p.catch((err) => {{
                    console.log("audio play failed:", err);
                }});
            }}
        }}
        </script>
        """,
        height=70,
    )


def _chunk_flow_texts(script, start_idx: int = 0, *, max_chars: int = 900, max_lines: int = 20):
    chunks = []
    start_idx = max(0, int(start_idx))
    pending = []
    pending_labels = []
    pending_chars = 0

    def flush():
        nonlocal pending, pending_labels, pending_chars
        if not pending:
            return
        label = pending_labels[0] if len(pending_labels) == 1 else f"{pending_labels[0]}-{pending_labels[-1]}"
        text = "\n".join(pending).strip()
        if text:
            chunks.append({"label": label, "text": text})
        pending = []
        pending_labels = []
        pending_chars = 0

    for i in range(start_idx, len(script)):
        line = script[i]
        text = str(line.get("text", "") or "").strip()
        if not text:
            continue
        if is_pause_only_text(text):
            text = "……"
        label = f"{i+1:04d}"
        add_len = len(text) + (1 if pending else 0)
        if pending and (pending_chars + add_len > max_chars or len(pending) >= max_lines):
            flush()
        pending.append(text)
        pending_labels.append(label)
        pending_chars += add_len
    flush()
    return chunks


@st.cache_data(show_spinner=False)
def build_flow_read_playlist(script, voice="ja-JP-NanamiNeural", rate="+15%", start_idx: int = 0):
    items = []
    for chunk in _chunk_flow_texts(script, start_idx=start_idx):
        audio_bytes = synthesize_tts(chunk["text"], voice=voice, rate=rate)
        if not audio_bytes:
            continue
        items.append(
            {
                "type": "audio",
                "src": "data:audio/mp3;base64," + base64.b64encode(audio_bytes).decode(),
                "label": chunk["label"],
                "text": chunk["text"],
            }
        )
    return items


def play_audio_playlist(items, *, voice: str = "", rate: str = ""):
    if not items:
        st.warning("流し読みできる音声がありません。")
        return

    st.session_state.audio_render_nonce += 1
    nonce = st.session_state.audio_render_nonce
    payload = json.dumps(items, ensure_ascii=False)

    components.html(
        f"""
        <div style="padding:8px 0;">
            <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap;">
                <button id="flow-start-{nonce}" style="padding:10px 18px; border:none; border-radius:10px; background:#2563eb; color:#fff; font-weight:700;">▶ START</button>
                <button id="flow-stop-{nonce}" style="padding:10px 18px; border:none; border-radius:10px; background:#0f172a; color:#fff; font-weight:700;">■ STOP</button>
            </div>
            <audio id="flow-audio-{nonce}" controls style="width:100%;"></audio>
            <div id="flow-status-{nonce}" style="font-size:0.95rem; margin-top:8px; color:#334155;">START を押すと流し読みします。</div>
            <div id="flow-current-{nonce}" style="font-size:1rem; margin-top:8px; color:#0f172a; line-height:1.7; white-space:pre-wrap;"></div>
        </div>

        <script>
        const queue = {payload};
        const audio = document.getElementById("flow-audio-{nonce}");
        const status = document.getElementById("flow-status-{nonce}");
        const current = document.getElementById("flow-current-{nonce}");
        const startBtn = document.getElementById("flow-start-{nonce}");
        const stopBtn = document.getElementById("flow-stop-{nonce}");
        let idx = 0;
        let stopped = true;
        let started = false;

        function setStatus(text) {{
            if (status) status.textContent = text;
        }}

        function setCurrent(text) {{
            if (current) current.textContent = text || "";
        }}

        function hardStop(message) {{
            stopped = true;
            try {{
                audio.pause();
                audio.removeAttribute("src");
                audio.load();
            }} catch (e) {{}}
            if (message) setStatus(message);
        }}

        function playNext() {{
            if (stopped) return;
            if (idx >= queue.length) {{
                hardStop("流し読みが終わりました。もう一度 START を押すと先頭から再生します。");
                setCurrent("");
                idx = 0;
                started = false;
                return;
            }}

            const item = queue[idx];
            const currentNo = idx + 1;
            idx += 1;
            setStatus(`再生中 ${{item.label}} (${{currentNo}}/${{queue.length}})`);
            setCurrent(item.text || "");
            audio.src = item.src;
            const p = audio.play();
            if (p !== undefined) {{
                p.catch((err) => {{
                    console.log("flow audio play failed:", err);
                    hardStop("再生に失敗しました。もう一度 START を押してください。");
                }});
            }}
        }}

        function startSequence() {{
            if (!queue || queue.length === 0) {{
                setStatus("再生できる音声がありません。");
                return;
            }}
            idx = 0;
            stopped = false;
            started = true;
            playNext();
        }}

        startBtn.addEventListener("click", () => {{
            startSequence();
        }});

        stopBtn.addEventListener("click", () => {{
            hardStop("停止しました。START を押すと先頭から再生します。");
            started = false;
            idx = 0;
        }});

        audio.onended = () => {{
            if (!stopped) playNext();
        }};

        window.addEventListener("beforeunload", () => hardStop(""));
        </script>
        """,
        height=190,
    )


def play_audio_and_click_next(audio_bytes: bytes, trigger_label: str):
    if not audio_bytes:
        return
    st.session_state.audio_render_nonce += 1
    nonce = st.session_state.audio_render_nonce
    b64 = base64.b64encode(audio_bytes).decode()
    safe_label = trigger_label.replace('"', '\"')
    components.html(
        f"""
        <audio id="auto-audio-{nonce}" autoplay controls style="width:100%;">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        <script>
        const audio = document.getElementById("auto-audio-{nonce}");
        const triggerLabel = "{safe_label}";
        const clickNext = () => {{
            const doc = window.parent.document;
            const buttons = Array.from(doc.querySelectorAll("button"));
            const target = buttons.find(btn => btn.innerText && btn.innerText.trim() === triggerLabel);
            if (target) {{
                target.click();
                return true;
            }}
            return false;
        }};
        if (audio) {{
            const p = audio.play();
            if (p !== undefined) {{
                p.catch((err) => console.log("auto audio play failed:", err));
            }}
            audio.onended = () => {{
                let tries = 0;
                const timer = setInterval(() => {{
                    if (clickNext() || tries > 20) {{
                        clearInterval(timer);
                    }}
                    tries += 1;
                }}, 150);
            }};
        }}
        </script>
        """,
        height=70,
    )


_PAUSE_ONLY_RE = re.compile(r'^[\s・･･･…‥\.。]+$')


def is_pause_only_text(text: str) -> bool:
    text = (text or '').strip()
    if not text:
        return False
    return bool(_PAUSE_ONLY_RE.fullmatch(text))


def estimate_pause_ms(text: str) -> int:
    text = (text or '').strip()
    if not text:
        return 900
    units = sum(1 for ch in text if ch in '・.。')
    units += sum(2 for ch in text if ch in '…‥')
    if units <= 0:
        units = max(1, len(text))
    return max(900, min(1800, 350 * units))


def click_next_after_delay(trigger_label: str, delay_ms: int):
    st.session_state.audio_render_nonce += 1
    nonce = st.session_state.audio_render_nonce
    safe_label = trigger_label.replace('"', '\"')
    delay_ms = max(0, int(delay_ms))
    components.html(
        f"""
        <div id="pause-next-{nonce}" style="height:1px;"></div>
        <script>
        const triggerLabel = "{safe_label}";
        const clickNext = () => {{
            const doc = window.parent.document;
            const buttons = Array.from(doc.querySelectorAll("button"));
            const target = buttons.find(btn => btn.innerText && btn.innerText.trim() === triggerLabel);
            if (target) {{
                target.click();
                return true;
            }}
            return false;
        }};
        setTimeout(() => {{
            let tries = 0;
            const timer = setInterval(() => {{
                if (clickNext() || tries > 20) {{
                    clearInterval(timer);
                }}
                tries += 1;
            }}, 150);
        }}, {delay_ms});
        </script>
        """,
        height=1,
    )


def prefetch_next_tts(script, current_idx: int, voice: str, rate: str):
    next_idx = current_idx + 1
    if next_idx >= len(script):
        return
    next_line = script[next_idx]
    next_text = next_line.get("text", "")
    prefetch_key = f"{next_idx}:{voice}:{rate}:{next_text}"
    if st.session_state.get("ai_prefetched_key") == prefetch_key:
        return
    try:
        synthesize_tts(next_text, voice=voice, rate=rate)
        st.session_state.ai_prefetched_key = prefetch_key
    except Exception:
        pass


def pcm_bytes_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 48000, channels: int = 1, sample_width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def audio_frame_to_mono_int16(frame):
    arr = frame.to_ndarray()
    arr = np.array(arr)
    if arr.ndim == 2:
        if arr.shape[0] <= 2:
            arr = arr.mean(axis=0)
        else:
            arr = arr.mean(axis=1)
    arr = np.squeeze(arr)
    if arr.dtype != np.int16:
        arr = arr.astype(np.int16)
    return arr


def collect_webrtc_audio(ctx, rms_threshold: int = 350):
    if not WEBRTC_AVAILABLE or ctx is None or not getattr(ctx.state, "playing", False):
        return False
    receiver = getattr(ctx, "audio_receiver", None)
    if receiver is None:
        return False
    try:
        frames = receiver.get_frames(timeout=0.2)
    except Exception:
        return False
    got_any = False
    now = time.time()
    for frame in frames:
        got_any = True
        mono = audio_frame_to_mono_int16(frame)
        if mono.size == 0:
            continue
        st.session_state.webrtc_sample_rate = getattr(frame, "sample_rate", 48000) or 48000
        st.session_state.webrtc_pcm_buffer += mono.tobytes()
        rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
        if rms >= rms_threshold:
            st.session_state.webrtc_speech_started = True
            st.session_state.webrtc_last_voice_ts = now
    return got_any


def maybe_finalize_webrtc_recording(expected: str, role: str, practice_mode: str, confirmed_script, user_role: str, apply_judgment_result):
    if not st.session_state.webrtc_speech_started:
        return False
    last_voice = st.session_state.webrtc_last_voice_ts
    if last_voice is None or time.time() - last_voice < 1.0:
        return False
    pcm_bytes = st.session_state.webrtc_pcm_buffer
    if not pcm_bytes:
        return False
    turn_key = f"{st.session_state.idx}:{len(pcm_bytes)}:{st.session_state.webrtc_last_voice_ts}"
    if st.session_state.webrtc_last_processed_turn == turn_key:
        return False
    wav_bytes = pcm_bytes_to_wav_bytes(
        pcm_bytes,
        sample_rate=st.session_state.webrtc_sample_rate or 48000,
        channels=1,
        sample_width=2,
    )
    transcript = transcribe_audio_bytes(wav_bytes)
    apply_judgment_result(expected, transcript, role, practice_mode, confirmed_script, user_role)
    st.session_state.webrtc_last_processed_turn = turn_key
    reset_webrtc_turn_state(st.session_state)
    return True


def transcribe_audio_bytes(audio_bytes: bytes) -> str:
    if not SR_AVAILABLE:
        raise RuntimeError("SpeechRecognition が未インストールです。")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(audio_bytes)
        wav_path = f.name
    try:
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio, language="ja-JP")
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


__all__ = [
    "EDGE_TTS_AVAILABLE",
    "SR_AVAILABLE",
    "WEBRTC_AVAILABLE",
    "webrtc_streamer",
    "WebRtcMode",
    "synthesize_tts",
    "play_audio_immediately",
    "play_audio_and_click_next",
    "build_flow_read_playlist",
    "play_audio_playlist",
    "is_pause_only_text",
    "estimate_pause_ms",
    "click_next_after_delay",
    "prefetch_next_tts",
    "collect_webrtc_audio",
    "maybe_finalize_webrtc_recording",
    "transcribe_audio_bytes",
]
