# Prompt untuk agent Windows

Bangun versi Mousehare tepat pada commit `4e4e6c175c7594ab5560768df893e4e1c76c9679` (26 Okt 2026, 03.35 WIB). Gunakan snapshot pada folder `V-senin-3:35`; jangan membangun folder root atau `V!`/`V3`.

Sinkronkan branch kerja terbaru:

```powershell
git fetch origin
git switch codex/mouseshare-core-refresh
git pull --ff-only origin codex/mouseshare-core-refresh
Set-Location -LiteralPath 'V-senin-3:35'
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

Pastikan file `V-senin-3:35\dist\Mousehare\Mousehare.exe` dan `V-senin-3:35\release\Mousehare-Setup-1.0.0.exe` terbentuk. Laporkan status build, ukuran dan lokasi installer, hasil `Get-AuthenticodeSignature`, serta error jika ada. Build default unsigned; jangan mengklaim lolos Smart App Control tanpa sertifikat penerbit tepercaya. Jangan push perubahan kode; kirim patch dan log ke PM.
