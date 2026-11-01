# PyInstaller-recept voor de Mac-app. Gebruik: bash packaging/build_macos.sh
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from open_fujitransfer import APP_NAME, __version__  # noqa: E402

ICON = os.path.join(ROOT, "build", "icon.icns")

a = Analysis([os.path.join(ROOT, "app.py")], pathex=[ROOT])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME,
          console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

if sys.platform == "darwin":  # BUNDLE bestaat alleen op macOS
  app = BUNDLE(
      coll,
      name=f"{APP_NAME}.app",
      icon=ICON if os.path.exists(ICON) else None,
      bundle_identifier="nl.dario.openfujitransfer",
      version=__version__,
      info_plist={
          "CFBundleDisplayName": APP_NAME,
          "CFBundleShortVersionString": __version__,
          "NSHighResolutionCapable": True,
          # Sinds macOS 15 vraagt een app toestemming voor het lokale netwerk;
          # zonder deze tekst mag hij de camera (192.168.0.1) niet bereiken.
          "NSLocalNetworkUsageDescription":
              "Open FujiTransfer maakt verbinding met je Fujifilm-camera via WiFi "
              "om foto's over te zetten.",
          "LSApplicationCategoryType": "public.app-category.photography",
      },
  )
