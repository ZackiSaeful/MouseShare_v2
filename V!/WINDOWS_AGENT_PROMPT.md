# Perintah build untuk agent Windows

Kerjakan build Windows untuk snapshot Mousehare pada folder `V!`, yaitu kondisi kode tepat sebelum commit `aa21df7` (30 Okt 2026, 19.26 WIB). Jangan membangun folder utama atau V3.

1. Sinkronkan branch `codex/mouseshare-core-refresh` dari origin:

   ```powershell
   git fetch origin
   git switch codex/mouseshare-core-refresh
   git pull --ff-only origin codex/mouseshare-core-refresh
   ```

2. Jalankan build dari folder snapshot:

   ```powershell
   Set-Location 'V!'
   powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
   ```

3. Pastikan terbentuk `V!\dist\Mousehare\Mousehare.exe` dan `V!\release\Mousehare-Setup-1.0.0.exe`. Jalankan installer/app dan catat error jika ada.
4. Laporkan hasil build, ukuran dan lokasi installer, status tanda tangan, serta pesan Smart App Control. Build default tidak ditandatangani; jangan klaim lolos Smart App Control tanpa sertifikat penerbit tepercaya.
5. Jangan push perubahan. Jika perlu memperbaiki skrip, kirim patch dan log build kepada PM sebelum mengubah kode snapshot.
