import { defineConfig } from 'vite';

export default defineConfig({
  // ffmpeg.wasm startet eigene Worker und darf nicht vorgebündelt werden
  optimizeDeps: {
    exclude: ['@ffmpeg/ffmpeg', '@ffmpeg/util'],
  },
});
