# Komando untuk agent Windows

Kamu adalah agent eksekusi Windows untuk Mousehare V3. Kerjakan hanya di folder `V3/` pada branch yang ditentukan; jangan ubah aplikasi V2 atau file Mac. Ikuti perintah PM berikut:

1. Ambil branch `codex/mouseshare-core-refresh` terbaru dari repository. Buka `V3/README.md` dan file ini sebelum mengubah apa pun. V3 adalah aplikasi baru dengan protokol baru; jangan mencampur file V2.
2. Di PowerShell, jalankan `cd V3`, lalu `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1` pada Windows 10/11 x64 dengan Python 3.12 dan Inno Setup 6.
3. Pastikan hasilnya ada: `V3/dist/MousehareV3/MousehareV3.exe` dan `V3/release/MousehareV3-Setup-3.0.0.exe`. Jalankan executable, pilih **Start receiving**, catat LAN IP, lalu uji bind port TCP 5555.
4. Bila ada sertifikat penerbit tepercaya yang disediakan organisasi, berikan thumbprint-nya ke skrip: `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1 -CertificateThumbprint "THUMBPRINT"`. Jangan membuat atau mengklaim sertifikat self-signed menyelesaikan Smart App Control.
5. Laporkan hasil build, lokasi installer, status tanda tangan (`Get-AuthenticodeSignature`), pesan Smart App Control bila ada, IP/port yang tampil, dan log/error lengkap. Jangan mengirim kredensial atau private key.
6. Jangan push perubahan sendiri. Kirim laporan dan patch yang diperlukan ke PM agar kode Mac dan Windows tetap satu komando.

Perintah penerimaan: build Windows harus selesai menghasilkan folder aplikasi dan installer EXE; kemudian pairing Mac V3 ke Windows V3 diuji pada dua komputer. Build di Mac tidak menggantikan build native Windows.
