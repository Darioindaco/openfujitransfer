"""Tekent assets/icon.png (1024x1024). Draai opnieuw als je het icoon wilt aanpassen:
    python3 assets/make_icon.py        (vereist Pillow)
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Achtergrond: donker paneel met afgeronde hoeken (macOS-raster: 824px binnen 1024)
m = 100
d.rounded_rectangle([m, m, S - m, S - m], radius=185, fill=(32, 33, 36))

# Camerabody
d.rounded_rectangle([230, 360, 794, 700], radius=48, fill=(236, 236, 230))
d.rounded_rectangle([300, 300, 470, 380], radius=22, fill=(236, 236, 230))   # zoeker-bult
d.rounded_rectangle([600, 318, 700, 372], radius=14, fill=(236, 236, 230))   # draaiknop
# Lens
d.ellipse([382, 400, 642, 660], fill=(32, 33, 36))
d.ellipse([422, 440, 602, 620], fill=(70, 72, 78))
d.ellipse([462, 480, 562, 580], fill=(32, 33, 36))
# Rood "aan"-lampje
d.ellipse([700, 400, 750, 450], fill=(192, 54, 44))

# WiFi-boogjes boven de camera
for i, r in enumerate((70, 120, 170)):
    cx, cy = 512, 300
    d.arc([cx - r, cy - r - 60, cx + r, cy + r - 60], start=225, end=315,
          fill=(192, 54, 44), width=26)

# Pijl omlaag (overzetten)
d.polygon([(512, 860), (432, 770), (592, 770)], fill=(192, 54, 44))
d.rectangle([487, 715, 537, 775], fill=(192, 54, 44))

out = Path(__file__).with_name("icon.png")
img.save(out)
print("geschreven:", out)
