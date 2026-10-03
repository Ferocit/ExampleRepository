import { FFmpeg } from '@ffmpeg/ffmpeg';
import coreURL from '@ffmpeg/core?url';
import wasmURL from '@ffmpeg/core/wasm?url';

const DEFAULT_LENGTH = 5;
// Der letzte Frame im Bereich [start, end) liegt knapp vor `end`
const END_EPSILON = 0.001;

const $ = (id) => document.getElementById(id);
const app = $('app');
const fileInput = $('file-input');
const fileName = $('file-name');
const mainVideo = $('main-video');
const startVideo = $('start-video');
const endVideo = $('end-video');
const startLabel = $('start-label');
const endLabel = $('end-label');
const totalLabel = $('total-label');
const track = $('track');
const selection = $('selection');
const playhead = $('playhead');
const startInput = $('start-input');
const lengthInput = $('length-input');
const fromPlayheadButton = $('from-playhead');
const playRangeButton = $('play-range');
const exportButton = $('export');
const statusEl = $('status');
const progressEl = $('progress');

const state = {
  file: null,
  url: null,
  duration: 0,
  start: 0,
  length: DEFAULT_LENGTH,
  stopAt: null,
};

let ffmpeg = null;

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return `${m}:${s.toFixed(3).padStart(6, '0')}`;
}

function setStatus(text) {
  statusEl.textContent = text;
}

// Setzt currentTime ohne Seek-Stau: während eines Seeks wird nur der letzte Wunsch gemerkt
function createSeeker(video) {
  let pending = null;
  video.addEventListener('seeked', () => {
    if (pending !== null) {
      const t = pending;
      pending = null;
      video.currentTime = t;
    }
  });
  return (t) => {
    if (video.seeking) {
      pending = t;
    } else {
      video.currentTime = t;
    }
  };
}

const seekStart = createSeeker(startVideo);
const seekEnd = createSeeker(endVideo);

function clampRange() {
  state.length = Math.min(Math.max(state.length, 0.1), state.duration);
  state.start = Math.min(Math.max(state.start, 0), state.duration - state.length);
}

function render() {
  if (!state.duration) return;
  const end = state.start + state.length;
  selection.style.left = `${(state.start / state.duration) * 100}%`;
  selection.style.width = `${(state.length / state.duration) * 100}%`;
  startLabel.textContent = formatTime(state.start);
  endLabel.textContent = formatTime(end);
  if (document.activeElement !== startInput) startInput.value = state.start.toFixed(3);
  if (document.activeElement !== lengthInput) lengthInput.value = state.length.toFixed(3);
  seekStart(state.start);
  seekEnd(Math.max(state.start, end - END_EPSILON));
}

function setRange(start, length = state.length) {
  state.start = start;
  state.length = length;
  clampRange();
  render();
}

function renderPlayhead() {
  if (!state.duration) return;
  playhead.style.left = `${(mainVideo.currentTime / state.duration) * 100}%`;
}

function loadFile(file) {
  if (!file) return;
  if (state.url) URL.revokeObjectURL(state.url);
  state.file = file;
  state.url = URL.createObjectURL(file);
  state.duration = 0;
  fileName.textContent = file.name;
  setStatus('');
  for (const v of [mainVideo, startVideo, endVideo]) v.src = state.url;
}

mainVideo.addEventListener('loadedmetadata', () => {
  if (!Number.isFinite(mainVideo.duration)) {
    setStatus('Die Videolänge kann nicht ermittelt werden. Bitte ein anderes Format versuchen.');
    return;
  }
  state.duration = mainVideo.duration;
  totalLabel.textContent = formatTime(state.duration);
  app.classList.remove('disabled');
  setRange(0, DEFAULT_LENGTH);
  renderPlayhead();
});

mainVideo.addEventListener('error', () => {
  app.classList.add('disabled');
  setStatus('Dieses Video kann der Browser nicht abspielen (Codec/Container nicht unterstützt).');
});

mainVideo.addEventListener('timeupdate', () => {
  renderPlayhead();
  if (state.stopAt !== null && mainVideo.currentTime >= state.stopAt) {
    mainVideo.pause();
    state.stopAt = null;
  }
});
mainVideo.addEventListener('seeked', renderPlayhead);
mainVideo.addEventListener('pause', () => {
  state.stopAt = null;
});

fileInput.addEventListener('change', () => loadFile(fileInput.files[0]));
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  const file = [...e.dataTransfer.files].find((f) => f.type.startsWith('video/'));
  loadFile(file);
});

// Zeitstrahl: Auswahl ziehen, oder außerhalb klicken, um den Bereich dort beginnen zu lassen
function timeAt(clientX) {
  const rect = track.getBoundingClientRect();
  return ((clientX - rect.left) / rect.width) * state.duration;
}

let dragOffset = null;

track.addEventListener('pointerdown', (e) => {
  if (!state.duration) return;
  const t = timeAt(e.clientX);
  if (e.target === selection) {
    dragOffset = t - state.start;
  } else {
    setRange(t);
    dragOffset = 0;
  }
  track.setPointerCapture(e.pointerId);
  selection.focus();
});

track.addEventListener('pointermove', (e) => {
  if (dragOffset === null) return;
  setRange(timeAt(e.clientX) - dragOffset);
});

const endDrag = () => {
  dragOffset = null;
};
track.addEventListener('pointerup', endDrag);
track.addEventListener('pointercancel', endDrag);

selection.addEventListener('keydown', (e) => {
  const step = e.shiftKey ? 1 : 0.1;
  if (e.key === 'ArrowLeft') setRange(state.start - step);
  else if (e.key === 'ArrowRight') setRange(state.start + step);
  else return;
  e.preventDefault();
});

startInput.addEventListener('change', () => {
  const v = parseFloat(startInput.value);
  if (Number.isFinite(v)) setRange(v);
  else render();
});

lengthInput.addEventListener('change', () => {
  const v = parseFloat(lengthInput.value);
  if (Number.isFinite(v)) setRange(state.start, v);
  else render();
});

fromPlayheadButton.addEventListener('click', () => setRange(mainVideo.currentTime));

playRangeButton.addEventListener('click', () => {
  mainVideo.currentTime = state.start;
  mainVideo.play().then(() => {
    state.stopAt = state.start + state.length;
  });
});

// Export mit ffmpeg.wasm. Neu kodieren statt Stream-Copy, damit der Schnitt framegenau ist
// und nicht auf den nächsten Keyframe springt.
async function getFFmpeg() {
  if (ffmpeg) return ffmpeg;
  setStatus('Lade ffmpeg (einmalig, ca. 30 MB) …');
  const instance = new FFmpeg();
  instance.on('progress', ({ progress }) => {
    progressEl.value = Math.min(Math.max(progress, 0), 1);
  });
  await instance.load({ coreURL, wasmURL });
  ffmpeg = instance;
  return ffmpeg;
}

function outputName(file, start, end) {
  const base = file.name.replace(/\.[^.]+$/, '');
  const fmt = (t) => t.toFixed(2).replace('.', '_');
  return `${base}_${fmt(start)}-${fmt(end)}.mp4`;
}

async function exportClip() {
  const { file, start, length } = state;
  const inputDir = '/input';
  const output = 'clip.mp4';
  exportButton.disabled = true;
  progressEl.hidden = false;
  progressEl.value = 0;
  let dirCreated = false;
  let mounted = false;
  try {
    const ff = await getFFmpeg();
    setStatus('Schneide Clip …');
    // WORKERFS liest direkt aus der Datei, statt das ganze Video in den Speicher zu kopieren
    await ff.createDir(inputDir);
    dirCreated = true;
    await ff.mount('WORKERFS', { files: [file] }, inputDir);
    mounted = true;
    const code = await ff.exec([
      '-ss', start.toFixed(3),
      '-i', `${inputDir}/${file.name}`,
      '-t', length.toFixed(3),
      '-map', '0:v:0',
      '-map', '0:a:0?',
      '-c:v', 'libx264',
      '-preset', 'veryfast',
      '-crf', '18',
      '-pix_fmt', 'yuv420p',
      '-c:a', 'aac',
      '-b:a', '192k',
      '-movflags', '+faststart',
      output,
    ]);
    if (code !== 0) throw new Error(`ffmpeg beendet mit Code ${code}`);
    const data = await ff.readFile(output);
    await ff.deleteFile(output);
    const url = URL.createObjectURL(new Blob([data.buffer], { type: 'video/mp4' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = outputName(file, start, start + length);
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    setStatus(`Gespeichert: ${a.download}`);
  } catch (err) {
    console.error(err);
    setStatus(`Fehler beim Export: ${err.message ?? err}`);
  } finally {
    if (mounted) await ffmpeg.unmount(inputDir);
    if (dirCreated) await ffmpeg.deleteDir(inputDir);
    exportButton.disabled = false;
    progressEl.hidden = true;
  }
}

exportButton.addEventListener('click', exportClip);
