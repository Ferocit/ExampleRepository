# Easy Cutter

Schlanke Desktop-App (Python + Tkinter), um einen Bereich aus einem Video framegenau als eigenen Clip zu speichern.

## Installation

Python 3.10 oder neuer mit Tkinter. Unter Linux ggf. `sudo apt install python3-tk`.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Start

```bash
python -m easy_cutter            # oder direkt mit Datei:
python -m easy_cutter video.mp4
```

## Bedienung

- **Video laden**
- Links das ganze Video mit Abspielen/Pause und Positionsregler, rechts erster und letzter Frame des Bereichs.
- Bereich (Default 5 s) auf dem Zeitstrahl ziehen oder neben die Auswahl klicken. Mit Fokus auf dem Zeitstrahl verschieben die Pfeiltasten um 0,1 s, mit Shift um 1 s.
- Start und Länge lassen sich auch als Zahl eingeben.
- **Start = aktuelle Position** übernimmt die Position des Players als Bereichsanfang.
- **Bereich abspielen** spielt nur den gewählten Bereich.
- **Clip speichern** exportiert als MP4 (H.264/AAC).

Der Bereich umfasst alle Frames ab `Start` bis vor `Start + Länge`. Bei 25 fps und 3 s bis 8 s ist der letzte Frame also der bei 7,96 s. Der exportierte Clip enthält genau die Frames, die rechts angezeigt werden.

## Technik und Grenzen

- Frames werden mit [PyAV](https://pyav.basswood-io.com/) dekodiert, exportiert wird mit ffmpeg. Liegt kein `ffmpeg` im PATH, wird das von `imageio-ffmpeg` mitgelieferte genutzt.
- Beim Export wird neu kodiert, damit der Schnitt nicht auf Keyframes springt. Das ist langsamer als ein Schnitt ohne Neukodierung.
- Die Wiedergabe im Hauptfenster ist ohne Ton und dient nur der Orientierung. Der exportierte Clip enthält den Ton.
- Kein Drag & Drop (Tkinter kann das nicht ohne Zusatzpaket).

## Tests

```bash
pip install pytest
pytest
```
