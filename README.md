# Easy Cutter

Einen Bereich aus einem Video framegenau als eigenen Clip speichern. Läuft komplett im Browser, das Video verlässt den Rechner nicht.

## Start

```bash
npm install
npm run dev
```

## Bedienung

- **Video laden** (Button oder Drag & Drop)
- Bereich auf dem Zeitstrahl ziehen oder neben die Auswahl klicken. Pfeiltasten verschieben um 0,1 s, mit Shift um 1 s.
- Start und Länge (Default 5 s) lassen sich auch als Zahl eingeben.
- Rechts siehst du den ersten und den letzten Frame des Bereichs.
- **Clip speichern** exportiert den Bereich als MP4 (H.264/AAC).

## Technik und Grenzen

- Export mit [ffmpeg.wasm](https://github.com/ffmpegwasm/ffmpeg.wasm). Es wird neu kodiert, damit der Schnitt nicht auf Keyframes springt. Das ist langsamer als ein Stream-Copy (grob Echtzeit oder langsamer, je nach Auflösung).
- Die Vorschau kann nur Formate zeigen, die der Browser abspielen kann (MP4/H.264, WebM).
- ffmpeg.wasm lädt beim ersten Export ca. 30 MB.
