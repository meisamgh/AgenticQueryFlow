// Qwen transcribes a short recording; the user reviews the text before sending it.
const SAMPLE_RATE = 16000;
const MAX_CAPTURE_BYTES = 8 * 1024 * 1024;

export function encodeWav(audio, maxSeconds = 60) {
  const length = Math.min(Math.floor(audio.length * SAMPLE_RATE / audio.sampleRate), maxSeconds * SAMPLE_RATE);
  if (!length || !audio.numberOfChannels) throw new Error("The recording is empty. Please try again.");
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const label = (offset, value) => [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  label(0, "RIFF"); view.setUint32(4, 36 + length * 2, true);
  label(8, "WAVE"); label(12, "fmt "); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, SAMPLE_RATE, true); view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  label(36, "data"); view.setUint32(40, length * 2, true);
  const channels = Array.from({length: audio.numberOfChannels}, (_, i) => audio.getChannelData(i));
  const ratio = audio.sampleRate / SAMPLE_RATE;
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(audio.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let value = 0;
    for (const channel of channels) {
      for (let j = start; j < end; j++) value += channel[j];
    }
    value = Math.max(-1, Math.min(1, value / ((end - start) * channels.length)));
    view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return new Uint8Array(buffer);
}

const microphoneIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg>';
const stopIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

export class VoiceInput {
  constructor({button, cancelButton, input, status, onBusy}) {
    Object.assign(this, {button, cancelButton, input, status, onBusy});
    this.phase = "idle";
    this.queryBusy = false;
    this.configured = false;
    this.runId = 0;
    button.addEventListener("click", () => {
      if (this.phase === "recording") this.stop();
      else if (!this.busy) this.start();
    });
    cancelButton.addEventListener("click", () => this.cancel());
    window.addEventListener("pagehide", () => this.cancel());
    this.render();
  }

  get busy() { return this.phase !== "idle"; }

  async initialize() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia
        || !window.MediaRecorder || !(window.AudioContext || window.webkitAudioContext)) {
      this.button.title = "Microphone input needs a supported browser on localhost or HTTPS.";
      this.status.textContent = "Voice input is unavailable in this browser.";
      return;
    }
    try {
      const response = await fetch("/api/voice", {cache: "no-store"});
      if (!response.ok) throw new Error("Voice input configuration is unavailable.");
      const config = await response.json();
      this.configured = config.configured;
      this.maxSeconds = Math.min(60, config.max_duration_seconds || 60);
      this.button.title = this.configured ? "Record a question" : config.unavailable_reason;
      if (!this.configured) this.status.textContent = "Voice input is not configured yet.";
      this.render();
    } catch (error) {
      this.button.title = error.message;
      this.status.textContent = "Voice input is unavailable. You can still type a question.";
    }
  }

  setQueryBusy(busy) { this.queryBusy = busy; this.render(); }

  render() {
    const recording = this.phase === "recording";
    this.button.disabled = !this.configured || this.queryBusy || (this.busy && !recording);
    this.button.innerHTML = recording ? stopIcon : microphoneIcon;
    this.button.setAttribute("aria-label", recording ? "Stop recording and transcribe" : "Record a question");
    this.button.setAttribute("aria-pressed", String(recording));
    this.button.classList.toggle("recording", recording);
    this.button.classList.toggle("transcribing", this.phase === "transcribing");
    this.cancelButton.hidden = !this.busy;
    this.onBusy(this.busy);
  }

  setPhase(phase, message) {
    this.phase = phase;
    if (message !== undefined) this.status.textContent = message;
    this.render();
  }

  releaseMicrophone() {
    clearInterval(this.timer);
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
  }

  cancel() {
    this.runId++;
    this.controller?.abort();
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.releaseMicrophone();
    this.chunks = [];
    this.setPhase("idle", "Recording discarded.");
  }

  async start() {
    if (this.busy || this.queryBusy || !this.configured) return;
    const runId = ++this.runId;
    this.setPhase("requesting", "Allow microphone access to record your question.");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (runId !== this.runId) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]
        .find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? {mimeType} : {});
      this.recorder = recorder;
      this.chunks = [];
      let size = 0;
      recorder.addEventListener("dataavailable", event => {
        if (runId !== this.runId) return;
        size += event.data.size;
        if (size > MAX_CAPTURE_BYTES) {
          this.cancel();
          this.status.textContent = "The recording is too large. Try a shorter question.";
        } else if (event.data.size) this.chunks.push(event.data);
      });
      recorder.addEventListener("error", () => {
        if (runId !== this.runId) return;
        this.cancel();
        this.status.textContent = "Could not record your voice. Please try again.";
      });
      recorder.addEventListener("stop", () => {
        if (runId !== this.runId) return;
        const blob = new Blob(this.chunks, {type: recorder.mimeType});
        this.chunks = [];
        this.releaseMicrophone();
        this.transcribe(blob, runId);
      });
      recorder.start(1000);
      const started = performance.now();
      this.setPhase("recording", "Listening · 0s. Click stop when finished.");
      this.timer = setInterval(() => {
        const seconds = Math.floor((performance.now() - started) / 1000);
        if (seconds >= this.maxSeconds) this.stop();
        else this.status.textContent = `Listening · ${seconds}s / ${this.maxSeconds}s. Click stop when finished.`;
      }, 250);
    } catch (error) {
      if (runId !== this.runId) return;
      this.releaseMicrophone();
      const message = error.name === "NotAllowedError"
        ? "Microphone access was denied. Allow it in your browser to use voice input."
        : error.name === "NotFoundError" ? "No microphone was found." : "Could not start the microphone. Please try again.";
      this.setPhase("idle", message);
    }
  }

  stop() {
    if (this.recorder?.state !== "recording") return;
    clearInterval(this.timer);
    this.setPhase("transcribing", "Transcribing your question…");
    this.recorder.stop();
  }

  async transcribe(blob, runId) {
    this.setPhase("transcribing", "Transcribing your question…");
    let timeout;
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      const audioContext = new AudioContextClass();
      let wav;
      try {
        const decoded = await audioContext.decodeAudioData(await blob.arrayBuffer());
        wav = encodeWav(decoded, this.maxSeconds);
      } finally { await audioContext.close(); }
      if (runId !== this.runId) return;
      this.controller = new AbortController();
      timeout = setTimeout(() => this.controller.abort(), 125000);
      const response = await fetch("/api/voice/transcribe", {
        method: "POST", headers: {"Content-Type": "audio/wav"},
        body: new Blob([wav], {type: "audio/wav"}), signal: this.controller.signal,
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "Voice transcription failed. Please try again.");
      if (runId !== this.runId) return;
      if (!result.text?.trim()) {
        this.status.textContent = "No clear speech was detected. Please try again.";
        return;
      }
      this.input.value = [this.input.value.trim(), result.text.trim()].filter(Boolean).join(" ");
      this.input.dispatchEvent(new Event("input", {bubbles: true}));
      this.input.focus();
      const usage = result.token_usage || {};
      const tokens = usage.input_tokens == null && usage.output_tokens == null
        ? "" : ` · ${((usage.input_tokens || 0) + (usage.output_tokens || 0)).toLocaleString()} speech tokens`;
      this.status.textContent = `Transcript ready — review it and send${tokens}.`;
    } catch (error) {
      if (runId === this.runId) this.status.textContent = error.name === "AbortError"
        ? "Voice transcription timed out. Please try again." : error.message;
    } finally {
      clearTimeout(timeout);
      if (runId === this.runId) this.setPhase("idle");
    }
  }
}
