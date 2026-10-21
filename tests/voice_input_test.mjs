// Run with: deno test tests/voice_input_test.mjs
import {encodeWav, VoiceInput} from "../web/voice.js";

function assert(value, message = "Assertion failed") { if (!value) throw new Error(message); }
function audio(channels, sampleRate) {
  return {length: channels[0].length, numberOfChannels: channels.length, sampleRate,
    getChannelData: index => channels[index]};
}

Deno.test("WAV encoder resamples stereo to bounded mono 16-kHz PCM", () => {
  const bytes = encodeWav(audio([
    new Float32Array(48000).fill(1), new Float32Array(48000).fill(-0.5),
  ], 48000));
  const view = new DataView(bytes.buffer);
  assert(new TextDecoder().decode(bytes.slice(0, 4)) === "RIFF");
  assert(view.getUint16(22, true) === 1);
  assert(view.getUint32(24, true) === 16000);
  assert(view.getUint16(34, true) === 16);
  assert(bytes.length === 44 + 16000 * 2);
  assert(Math.abs(view.getInt16(44, true) - 8192) <= 1);
  assert(encodeWav(audio([new Float32Array(32000)], 16000), 1).length === bytes.length);
});

Deno.test("WAV encoder clips extreme samples and rejects an empty recording", () => {
  const bytes = encodeWav(audio([new Float32Array([2, -2])], 16000));
  const view = new DataView(bytes.buffer);
  assert(view.getInt16(44, true) === 32767);
  assert(view.getInt16(46, true) === -32768);
  let rejected = false;
  try { encodeWav(audio([new Float32Array(0)], 16000)); } catch { rejected = true; }
  assert(rejected);
});

function composer() {
  const element = () => ({
    addEventListener() {}, setAttribute() {}, classList: {toggle() {}},
  });
  const input = {value: "Existing question", dispatchEvent() {}, focus() {this.focused = true;}};
  const status = {textContent: ""};
  const voice = new VoiceInput({button: element(), cancelButton: element(), input, status, onBusy() {}});
  voice.maxSeconds = 60;
  return {voice, input, status};
}

function fakeWindow() {
  return {
    addEventListener() {},
    AudioContext: class {
      async decodeAudioData() { return audio([new Float32Array(1600)], 16000); }
      async close() {}
    },
  };
}

Deno.test("voice transcription appends editable text and never submits a database query", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = fakeWindow();
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push(path);
    assert(options.headers["Content-Type"] === "audio/wav");
    assert((await options.body.arrayBuffer()).byteLength === 3244);
    return Response.json({text: "EUR customers", token_usage: {input_tokens: 11, output_tokens: 4}});
  };
  try {
    const {voice, input, status} = composer();
    await voice.transcribe(new Blob(["recording"]), voice.runId);
    assert(input.value === "Existing question EUR customers");
    assert(input.focused);
    assert(!voice.busy);
    assert(calls.length === 1 && calls[0] === "/api/voice/transcribe");
    assert(status.textContent.includes("15 speech tokens"));
  } finally { globalThis.window = originalWindow; globalThis.fetch = originalFetch; }
});

Deno.test("discarding an in-flight transcript preserves the question", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = fakeWindow();
  let started;
  const requestStarted = new Promise(resolve => { started = resolve; });
  let respond;
  globalThis.fetch = () => {
    started();
    return new Promise(resolve => { respond = resolve; });
  };
  try {
    const {voice, input} = composer();
    const transcription = voice.transcribe(new Blob(["recording"]), voice.runId);
    await requestStarted;
    voice.cancel();
    respond(Response.json({text: "Do not append this"}));
    await transcription;
    assert(input.value === "Existing question");
    assert(!voice.busy);
  } finally { globalThis.window = originalWindow; globalThis.fetch = originalFetch; }
});
