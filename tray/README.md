# Galaxy Buds Multipoint Fix — Windows Tray App

Compacte Windows Python-app die automatisch `budsmp apply` (`asVer=2` via `SPPSERVICE4` RFCOMM) uitvoert zodra gekoppelde Galaxy Buds verbinden. **Volledig bedienbaar vanuit de systeemtray — geen CLI nodig.**

- Dubbelklik `app.py` / `uv run app.py` / later `BudsFix.exe` → systeemtray-icoon
- Rechtsklik tray voor alle acties (status, fix, autorun, uninstall)
- Start optioneel automatisch bij login (Task Scheduler)
- Klein tkinter-statusvenster met live info
- Volledig lokaal, geen netwerk/telemetry, logs alleen `%LOCALAPPDATA%`

## Gebruik: alleen tray (geen CLI)

```powershell
cd tray
uv sync
uv run app.py                 # toont direct tray-icoon (geen console nodig)
# of zonder uv:
pip install pystray Pillow
python app.py                 # of pythonw app.py voor geen consolevenster
# later als exe:
BudsFix.exe                   # zelfde: tray verschijnt
```

Rechtsklik het blauwe **B**-icoon in de systeemtray:

| Menu item | Wat het doet |
|---|---|
| **Open** | Toont statusvenster (gekoppeld/verbonden/fix/autorun) |
| **Check Buds status** | Leest `asVer` uit Buds (via RFCOMM) en toont multipoint-status |
| **Run fix now** | Past fix direct toe (`asVer=2`) |
| **Revert fix** | Herstelt stock gedrag (`asVer=0`) |
| **Autorun inschakelen** | Maakt Task Scheduler taak `GalaxyBudsMultipointFix` (`onlogon`) |
| **Autorun uitschakelen** | Verwijdert taak / Startup-item / Registry key |
| **Uninstall (volledig verwijderen)** | Verwijdert autorun + logs + temp, toont wat verwijderd is |
| **Sluiten** | Sluit tray + background monitor (autorun blijft behouden) |

Autorun-items zijn automatisch disabled/enabled op basis van of autorun al aan staat.

In het statusvenster (Open) zie je bovendien knoppen voor dezelfde acties:
`Check Buds status`, `Run fix now`, `Revert fix`, `Autorun inschakelen/uitschakelen`.

## Autorun details

Tray > **Autorun inschakelen** maakt (voorkeur) een Task Scheduler taak:
```
GalaxyBudsMultipointFix  /sc onlogon  /rl limited  /it
  -> "pythonw.exe" "…\tray\app.py" --minimized
```
Handmatig checken:
```powershell
schtasks /query /tn "GalaxyBudsMultipointFix" /v
```
Fallbacks (bij geblokkeerde Task Scheduler): `.bat` in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` en `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

**Autorun uitschakelen** verwijdert alle drie. **Sluiten** laat autorun intact — bij volgende login start de tray opnieuw.

## Uninstall

Tray > **Uninstall (volledig verwijderen)** verwijdert: taak, Startup-items, Registry key, logs in `%LOCALAPPDATA%\GalaxyBudsMultipoint` en `tray\logs`, temp `budsmp-wake-*.wav`. Toont dialoog wat verwijderd is. De app/exe zelf blijft staan — handmatig verwijderen indien gewenst.

## Reconnect / case / power-cycle

`asVer` leeft in RAM op de Buds en wordt gewist bij power-down in de case (`docs/asver-lifetime.md`). De monitor pollt elke 3s `Get-PnpDevice`; bij `disconnected → connected` wordt de fix (20s debounce) opnieuw uitgevoerd. Disconnect wist de debounce zodat een case-cyclus altijd opnieuw fixt — je merkt er niets van.

## Veiligheid

- Alleen RFCOMM write `version_only(2)` naar `SPPSERVICE4` ch29, verder geen Bluetooth-actie.
- Reversible via tray > Revert fix (`asVer=0`).
- Standaard Python + `pystray`/`Pillow`; `tkinter` is stdlib; 100% lokaal.

## Voor gevorderden (optioneel CLI)

CLI is niet nodig voor normaal gebruik, maar blijft beschikbaar voor debug:
```powershell
uv run app.py --minimized   # verborgen start (voor taak/exe)
uv run app.py --verbose     # debug logging
uv run app.py --status      # eenmalige status in console (hidden flag)
```
