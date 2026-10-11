// Offline speech-to-text in the browser (Transformers.js + Whisper ONNX), runs in a module worker so the
// UI stays responsive. Everything is served by support-ui; nothing is fetched from the internet.
//   postMessage({type:"load", model:"whisper-base"})
//   postMessage({type:"transcribe", audio:Float32Array(16 kHz mono), language:"russian"})
// replies: {type:"progress", status, file, progress} | {type:"ready", device} | {type:"result", text} | {type:"error", message}
import { pipeline, env } from "/static/vendor/transformers.min.js";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "/static/models/";
env.backends.onnx.wasm.wasmPaths = {
  mjs: "/static/vendor/ort-wasm-simd-threaded.asyncify.mjs",
  wasm: "/static/vendor/ort-wasm-simd-threaded.asyncify.wasm",
};

let asr = null, device = null;

async function load(model) {
  const attempts = [];
  if (navigator.gpu) attempts.push({ device: "webgpu", dtype: { encoder_model: "fp32", decoder_model_merged: "q4" } });
  attempts.push({ device: "wasm", dtype: { encoder_model: "q8", decoder_model_merged: "q8" } });
  let lastErr;
  for (const a of attempts) {
    try {
      asr = await pipeline("automatic-speech-recognition", model, {
        device: a.device, dtype: a.dtype,
        progress_callback: p => postMessage({ type: "progress", status: p.status, file: p.file, progress: p.progress }),
      });
      device = a.device;
      postMessage({ type: "ready", device });
      return;
    } catch (e) { lastErr = e; asr = null; }
  }
  throw lastErr;
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === "load") { if (!asr) await load(data.model); else postMessage({ type: "ready", device }); }
    if (data.type === "transcribe") {
      if (!asr) throw new Error("model not loaded");
      const out = await asr(data.audio, { language: data.language || "russian", task: "transcribe", chunk_length_s: 30, stride_length_s: 5, return_timestamps: false });
      postMessage({ type: "result", text: (out.text || "").trim() });
    }
  } catch (e) { postMessage({ type: "error", message: String(e?.message || e) }); }
};
