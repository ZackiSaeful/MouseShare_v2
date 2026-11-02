# Prompt untuk agent Windows

Bangun versi Mousehare tepat pada commit `4e4e6c175c7594ab5560768df893e4e1c76c9679` (26 Okt 2026, 03.35 WIB). Gunakan snapshot pada folder `V-senin-3:35`; jangan membangun folder root atau `V!`/`V3`.

## Perbaikan handoff Windows → Mac

Sebelum build, pastikan snapshot yang dibangun memuat perbaikan berikut. Branch hasil sinkronisasi dapat belum memuat perubahan lokal PM, jadi terapkan perubahan ini pada `V-senin-3:35` setelah `git pull` dan kirim patch-nya kembali ke PM:

1. Di `mouseshare/windows/windows_app.py`, jangan filter event mouse sintetis dari `pynput.mouse.Listener`. Gerakan Mac diinjeksi sebagai event sintetis di Windows, dan listener perlu melihatnya agar bisa mendeteksi kursor mencapai tepi untuk kembali ke Mac. Hapus `win32_event_filter=_ignore_injected_mouse_event` dari `_start_mouse_listener` dan hapus helper beserta konstanta flag-nya bila tidak digunakan lagi.
2. Selesaikan konflik merge di `_handle_mouse_move`: pertahankan pengecekan `STATE.active`, lalu injeksikan gerakan dengan `_ms.move(dx, dy)`. Hapus semua marker konflik dan kode yang mengakses `STATE.injecting` karena atribut itu tidak tersedia.
3. Di `_on_mouse_move`, jika pengiriman pesan `edge` gagal, jangan mematikan `STATE.active`; reset debounce `_edge_sent` agar Windows tetap aktif dan bisa mencoba handoff lagi.
4. Perbarui catatan `mouseshare/windows/README.md` agar menjelaskan bahwa event sintetis harus diamati oleh listener untuk mendukung handoff balik.

Setelah build berhasil, lakukan reproduksi manual: hubungkan Mac dan Windows, gerakkan pointer dari Mac ke Windows, lalu gerakkan kembali melewati tepi Windows yang menghadap Mac. Pastikan kursor terlihat kembali di Mac dan status Windows kembali ke Mac Active. Catat hasil, posisi Mac yang dikonfigurasi, dan log `[EDGE]` dari kedua sisi. Jika agent hanya memiliki mesin Windows, koordinasikan uji alur akhir dengan PM; jangan klaim handoff end-to-end berhasil tanpa mengujinya.

Sinkronkan branch kerja terbaru:

```powershell
git fetch origin
git switch codex/mouseshare-core-refresh
git pull --ff-only origin codex/mouseshare-core-refresh
Set-Location -LiteralPath 'V-senin-3:35'
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

Pastikan file `V-senin-3:35\dist\Mousehare\Mousehare.exe` dan `V-senin-3:35\release\Mousehare-Setup-1.0.0.exe` terbentuk. Laporkan status build, ukuran dan lokasi installer, hasil `Get-AuthenticodeSignature`, serta error jika ada. Build default unsigned; jangan mengklaim lolos Smart App Control tanpa sertifikat penerbit tepercaya. Jangan push perubahan kode; kirim patch dan log ke PM.
