# Prompt untuk agent Windows — audio hanya di Mac dan kontrol di Home

Kerjakan di `production-v6`, cocokkan langsung dengan `mouseshare/audio_playback.py` dan `mouseshare/mac/mac_app.py` pada branch ini. Laporan sebelumnya menyebut commit `b08d746`, tetapi commit tersebut belum ada di riwayat Git workspace bersama. Bawa perubahan Windows itu ke workspace/branch bersama agar bisa ditinjau, jangan push.

## Tujuan utama

Saat fitur audio aktif dan Mac sudah opt-in, suara sistem Windows harus terdengar **hanya di Mac**. Windows tetap menjadi sumber audio, tetapi output lokal Windows harus senyap selama audio dirutekan ke Mac. Saat fitur mati, Mac opt-out, koneksi putus, capture gagal, atau aplikasi ditutup, pulihkan kondisi audio Windows seperti sebelum routing dimulai. Jangan capture mikrofon.

## Temuan reproduksi terbaru — fokus diagnosis sekarang

Di Mac, log sesi terbaru menunjukkan `Mac CoreAudio output is ready`, `First Windows PCM frame accepted`, dan `First non-silent Windows PCM frame queued to Mac CoreAudio`; pengguna juga mengonfirmasi suara memang terdengar di Mac. Bug yang tersisa adalah suara **masih terdengar dari speaker Windows secara bersamaan**. Fokuskan diagnosis pada Windows local-output mute dan versi executable yang sedang berjalan. Jangan menyimpulkan masalah Mac playback atau mengubah jalur PCM tanpa bukti baru.

Sebelum mengedit kode, periksa dan laporkan:

1. PID, command line, executable path, product/file version, dan SHA-256 dari `Mousehare.exe` yang sedang berjalan. Bandingkan dengan executable hasil build dan executable yang terpasang; pastikan pengguna benar-benar menjalankan build berisi mute Windows. Jangan menganggap build terbaru otomatis sedang dipakai.
2. Enumerasi Windows render endpoints yang aktif, termasuk `eRender/eConsole` default, endpoint ID, nama perangkat, state, mute, dan volume scalar. Catat endpoint ID yang dipakai WASAPI loopback capture lalu pastikan itu endpoint render speaker yang sedang mengeluarkan suara. Jangan mengirim perintah mute ke loopback/capture endpoint atau endpoint yang berbeda.
3. Telusuri seluruh lifecycle mute: penerimaan `audio_control:true`, start capture, pemanggilan mute, jalur cleanup `finally`, stop, opt-out, reconnect, exception, endpoint-change, serta callback volume. Temukan apakah mute tidak pernah dipanggil, gagal, salah endpoint, atau langsung dipulihkan.
4. Pada setiap transisi, log endpoint ID/nama, alasan transisi, nilai HRESULT/error dari operasi mute, lalu baca kembali `GetMute` dan volume endpoint sesudah operasi. Verifikasi juga notifikasi perubahan endpoint. Jangan menganggap flag internal `muted=True` sebagai bukti Windows benar-benar mute.
5. Ketika pengguna masih mendengar Windows, ambil status aktual endpoint dan sesi render yang aktif saat itu. Bedakan mute endpoint yang sukses dari state UI aplikasi; periksa apakah suara keluar melalui endpoint kedua (misalnya HDMI, Bluetooth, monitor, dock) atau endpoint default berubah setelah capture mulai.

Jangan membuat perubahan yang mematikan capture Mac. Mac sudah menerima PCM non-senyap, jadi lakukan uji yang mengukur dua hasil secara terpisah: (a) PCM tetap non-senyap sampai Mac, dan (b) endpoint fisik Windows yang sedang dipakai benar-benar senyap. Jika `GetMute=true` tetapi speaker fisik tetap berbunyi, identifikasi perangkat/driver dan gunakan cara perutean/mute yang benar-benar meniadakan output lokal pada perangkat itu; jangan melaporkan selesai berdasarkan status API saja.

Saat fitur ON, mute hanya setelah `audio_control:true` dari Mac, secure session aktif, dan capture/sender berjalan. Saat OFF atau jalur sesi berhenti, pulihkan status awal endpoint persis sekali. Jika mute gagal atau endpoint tidak dapat dipastikan, fail-safe: jangan meninggalkan Windows dalam keadaan mute; tampilkan error yang jelas dan laporkan hasil diagnosis.

## UI Windows

- Tambahkan kontrol ON/OFF audio di halaman **Home utama**, terlihat tanpa membuka Settings, dengan pola dan hierarki visual yang senada dengan kontrol audio pada Home Mac.
- Pertahankan kontrol yang sama di Settings → General bila sudah ada. Kedua kontrol harus membaca/menulis preferensi yang sama dan langsung menyinkronkan tampilannya.
- Label yang disarankan: **“Play audio on Mac only”** dengan keterangan ringkas bahwa output Windows dimute sementara ketika fitur benar-benar aktif, lalu dipulihkan saat berhenti.
- Status ON di halaman Home harus mencerminkan sesi aktif/opt-in; jika Windows sudah ON tetapi Mac belum tersambung atau belum opt-in, tampilkan status menunggu dan jangan mute Windows.
- Toggle tetap opt-in: default OFF. Jangan memulai capture atau mute sebelum sesi terenkripsi aktif dan kedua sisi ON.

Protokol yang harus cocok persis:
- Mac mengirim `{"type":"audio_control","enabled":true|false}` hanya setelah secure handshake. Perubahan toggle berlaku langsung.
- Mac hanya mengirim `enabled:true` setelah output CoreAudio berhasil dibuka. Perlakukan kontrol `true` sebagai konfirmasi bahwa Mac siap menerima playback; jangan mute Windows sebelum sinyal ini diterima. Jika Mac output gagal dibuka, Mac mengirim `enabled:false` agar Windows tetap terdengar lokal.
- Windows mengirim `{"type":"audio_pcm","pcm":"<base64>"}` hanya di `SecureChannel` terenkripsi; legacy session tidak boleh mengirim audio.
- WASAPI loopback, signed little-endian int16, 48 kHz, stereo, frame tepat 20 ms (960 sample/channel; 3.840 byte decoded; 5.120 karakter Base64).
- Capture hanya berjalan jika preferensi Windows `audio_share` ON, kontrol Mac terbaru ON, dan sesi terenkripsi aktif. Keduanya default OFF. Berhenti segera jika salah satu OFF, disconnect, error, atau shutdown.
- Audio yang dikirim ke Mac harus tetap memiliki gain independen di Mac; jangan mengubah level volume Windows untuk meniru slider volume Mac.
- Mute Windows hanya selama sesi audio benar-benar aktif: Windows preference ON, kontrol Mac terbaru ON, koneksi terenkripsi aktif, dan capture/sender berjalan. Jika belum memenuhi semua kondisi itu, output lokal Windows harus tetap seperti semula.
- Simpan status mute awal dari endpoint yang sedang dipakai. Jangan unmute endpoint yang memang sudah mute sebelum routing aktif. Pulihkan status semula tepat satu kali pada setiap jalur stop/error/disconnect/shutdown; tangani pergantian endpoint dengan aman.
- **Jangan mengasumsikan WASAPI loopback tetap berisi audio setelah endpoint dimute.** Microsoft mendokumentasikan bahwa loopback dapat terpengaruh endpoint volume/mute, dan titik tap dapat bergantung pada driver. Verifikasi pada perangkat nyata pengguna (terutama Realtek): saat output lokal dimute, Mac tetap menerima PCM non-senyap dan suara hanya terdengar di Mac. Jika endpoint mute membungkam loopback, gunakan routing yang mengambil sinyal sebelum mute atau desain lain yang andal. Jangan merilis perubahan yang hanya membuat Windows senyap tetapi Mac juga senyap.
- Jangan memakai perubahan mute/volume OS yang tidak dipulihkan pada crash. Tambahkan pemulihan saat shutdown normal dan mekanisme pemulihan aman jika proses berakhir mendadak, atau jelaskan batasnya dan gunakan pendekatan routing yang tidak meninggalkan endpoint dalam keadaan mute.
- Gunakan worker audio dan antrean kecil terbatas; drop frame usang saat penuh. Jangan menghambat event mouse/keyboard, handoff, atau GUI. Pertahankan `TCP_NODELAY`.
- UI Home dan Settings harus menjelaskan bahwa saat audio benar-benar tersambung, audio diputar di Mac dan output lokal Windows dimute sementara, lalu dipulihkan saat fitur berhenti.

Periksa kode dan log sebelum menyatakan fitur siap. Cocokkan nama fungsi yang dipanggil UI dengan fungsi aktual Mac/Windows. Jalankan uji ON/OFF saat video Windows diputar pada perangkat pengguna: ON harus menghasilkan suara di Mac dan senyap pada speaker Windows; OFF/disconnect harus memulihkan Windows. Catat secara terpisah bukti PCM non-senyap Mac dan state endpoint fisik Windows. Uji jika Windows awalnya mute, startup/error, endpoint berubah, crash/restart, dan disconnect under load. Pastikan tombol Home dan Settings sinkron, dan status menunggu tidak memute Windows. Build Windows, jalankan executable hasil build, pastikan process path menunjuk build terbaru, laporkan commit, file, endpoint ID/nama, HRESULT/GetMute sebelum-sesudah, apakah speaker benar-benar senyap, dan pemulihan setelah OFF. Jangan push.
