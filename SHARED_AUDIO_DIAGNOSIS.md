# Mousehare Shared Audio — Diagnosis dan Rancangan Awal

Tanggal diagnosis: 1 November 2026

## Ringkasan codebase

- Source app memakai Python 3.13 di macOS dan Python 3.12 di Windows, dengan GUI Tkinter. Entry point terpadu ada di `mouseshare_app.py`; backend platform terpisah di `mouseshare/mac/mac_app.py` dan `mouseshare/windows/windows_app.py`.
- Mac adalah client TCP; Windows adalah server TCP. Default port 5555. Koneksi menjalankan pairing X25519/HKDF, lalu semua pesan v3 dikirim sebagai JSON yang dilindungi AES-GCM dan sequence number.
- Frame transport berisi 4-byte big-endian length lalu JSON UTF-8; protokol membatasi frame ke 128 KiB, secure transport membatasi plaintext ke 64 KiB. Satu koneksi menangani input, clipboard, dan kontrol. Belum ada audio API atau dependensi audio.
- Pemrosesan jaringan berjalan di thread backend. Input Mac memakai event-tap callback dan antrean input tersendiri supaya callback tidak blocking. Integrasi audio sebaiknya tidak menyentuh state machine input atau callback event tap.
- Source Windows tersedia lokal sehingga review awal dapat dilakukan tanpa akses komputer Windows. Ini belum menggantikan uji aktual WASAPI loopback pada Windows.
- Workspace ini bukan checkout Git (`git status` gagal: bukan repository), sehingga status perubahan lokal dan riwayat cabang tidak dapat diperiksa dari sini.

## Kelayakan dan batas arsitektur

Menerima audio Windows dan mengatur gain Windows sendiri layak dilakukan tanpa virtual device di Mac: receiver memproses frame stereo, mengalikan gain Windows, lalu mengirim hasilnya ke output device melalui CoreAudio. CoreAudio akan mencampurkan stream aplikasi Mousehare ke output fisik bersama stream aplikasi Mac yang berjalan biasa. Mute Windows cukup mengatur gain stream ke nol.

Namun, kontrol Mac Audio independen adalah kebutuhan berbeda. Mengubah volume perangkat output Mac mengubah seluruh output fisik, termasuk audio Windows. Mengatur slider gain di Mousehare juga tidak akan mengubah stream Spotify/game Mac yang langsung dimainkan oleh aplikasi tersebut. Agar slider Mac benar-benar berfungsi dan kedua sumber dapat di-mute secara terpisah, Mousehare harus menangkap audio sistem Mac, mengatur gain-nya, lalu mencampurnya kembali dengan audio Windows sebelum output fisik.

Apple menyediakan Core Audio process taps (macOS 14.2+) untuk menangkap proses atau mix output sistem; tap dapat dimasukkan ke aggregate device dan memerlukan `NSAudioCaptureUsageDescription` serta izin perekaman audio sistem. `CATapMuteBehavior` mencakup mode yang mematikan direct output dari proses yang di-tap, memungkinkan pola capture-then-remix. Alternatif ScreenCaptureKit memerlukan izin Screen Recording dan punya model stream capture yang lebih besar. Virtual AudioServerPlugIn device bisa menyelesaikan routing, tetapi menambah driver, instalasi, signing/notarization, pemeliharaan lintas versi macOS, dan titik kegagalan. Karena kebutuhan belum membenarkan biaya driver, rekomendasi awal adalah uji Core Audio process tap + aggregate device secara native; jangan klaim slider Mac independen sebelum capture, mute-direct-output, remix, dan pemulihan device terbukti.

Implementasi audio produksi sebaiknya native Swift/Objective-C (CoreAudio/AudioUnit/AVAudioEngine) dengan antarmuka tipis ke UI Python, atau komponen native kecil yang dipaketkan bersama app. Callback audio real-time tidak boleh melakukan JSON, enkripsi, alokasi berulang, atau pekerjaan jaringan. Python/Tk tetap mengurus UI dan status.

## Rancangan yang direkomendasikan

1. **Windows capture**: WASAPI loopback capture pada default render endpoint, stereo. Konversi sample rate/channel ke format kontrak yang disepakati, jalankan encoder Opus, lalu kirim dari worker jaringan. Capture callback hanya menulis ke ring buffer bounded.
2. **Transport awal**: gunakan koneksi TCP dan secure channel Mousehare yang ada agar pairing, firewall port, dan autentikasi tetap satu sistem. Audio adalah stream data berurutan, bukan event input; alokasikan worker/queue audio bounded dan jangan mengirim dari callback WASAPI. Batasi ukuran tiap frame audio jauh di bawah 64 KiB plaintext. TCP memberi reliabilitas tetapi head-of-line blocking bisa menambah latensi; bila pengukuran menunjukkan masalah, desain tahap berikutnya adalah UDP dengan kunci sesi dan nonce/sequence anti-replay yang diturunkan secara eksplisit dari handshake v3. Jangan membuka UDP tanpa desain autentikasi.
3. **Kontrak audio v1**: pesan terenkripsi JSON `{type:"audio_format", version:1, codec:"opus", sample_rate:48000, channels:2, frame_ms:20}` sebelum audio. Tiap paket `{type:"audio_frame", stream_id, sequence, timestamp_ns, payload:<base64 Opus>}`; target 20 ms per paket, monotonik per stream. `audio_state` melaporkan running/stopped/error. Kontrol Mac ke Windows memakai `{type:"audio_control", enabled:boolean}`. Pesan `audio_format` dinegosiasikan/ditolak secara eksplisit. Hindari perubahan handshake hingga prototipe menunjukkan kebutuhan; semua pesan tetap melalui SecureChannel v3.
4. **Mac playback**: worker menerima/decode ke ring buffer jitter kecil yang bounded, resample bila sample rate hardware berbeda, lalu render callback CoreAudio/AudioUnit yang mencampur float stereo. Gain Windows dan mute diterapkan pada sample Windows sebelum sum. Mac gain/mute diterapkan ke captured Mac mix sebelum sum jika jalur system tap sudah tersedia. Clamp/limiter akhir untuk mencegah clipping. Perubahan gain atomik dan berlaku pada frame berikutnya.
5. **Output device**: kirim mix eksplisit ke default output device; observe device changes dan reconfigure engine tanpa menghentikan/merusak input connection. Periksa skenario headphone Bluetooth yang berganti sample rate.
6. **Resilience**: disconnect mengosongkan queue Windows, mute remote source, dan membiarkan Mac mix hidup. Reconnect otomatis mengikuti mekanisme TCP saat ini. Log jitter, underflow, late/drop count dan status perangkat tanpa mencatat isi audio.

### Independent gain

| Kontrol | Jalur implementasi | Dampak |
|---|---|---|
| Windows | Gain pada PCM hasil decode sebelum mixer; mute = gain 0 | Tidak mengubah volume Mac |
| Mac | Gain pada capture system mix sebelum mixer; mute = gain 0 | Memerlukan capture dan routing ulang seluruh audio Mac |
| Master (opsional) | Gain pada mix akhir sebelum output | Mengubah dua sumber bersama |
| Hardware | Volume output CoreAudio/device | Mengubah hasil mix keseluruhan; bukan kontrol sumber |

Core Audio taps tersedia menurut dokumentasi Apple untuk macOS 14.2 ke atas, sementara proyek mendokumentasikan dukungan mulai macOS 12. Keputusan produk perlu memilih: (a) pertahankan macOS 12 dengan Windows gain saja dan tandai Mac source gain unsupported di OS lama, atau (b) naikkan minimum macOS untuk memenuhi kontrak dua slider. Sebelum implementasi UI, uji prototype native pada minimum OS dan versi macOS yang benar-benar ditargetkan.

## Perubahan yang diperlukan

### Mac

- Modul audio engine terpisah dari `mac_app.py`: native capture/tap, output render, decode, mixer, ring buffer, gain/mute, device change handling.
- Receiver protocol menambahkan validasi/dispatch `audio_format`, `audio_frame`, `audio_state`; audio tidak boleh memblokir input message loop secara berkepanjangan.
- UI mixer Mac/Windows/Master + mute dan status koneksi/audio; pengiriman control gain lokal ke engine tanpa mengubah gain sistem.
- Permission string `NSAudioCaptureUsageDescription` dan panduan izin audio baru; perubahan plist/build packaging.
- Dependency/build native codec bila Opus dipakai; audit lisensi dan bundling.

### Windows

- Modul capture WASAPI loopback independen dari input listener/server GUI.
- Resample/format normalize, Opus encode, bounded ring buffer, worker send dan `audio_control` handling.
- Perluasan dispatcher jaringan dan status GUI/log untuk capture, encode, dan error perangkat.
- Dependency/binary Opus yang dapat dipaketkan pada Windows; update `requirements-windows.txt` serta PyInstaller hook/data bila perlu.
- Uji pada perangkat render nyata dan reconnect; tidak menyentuh input dispatch atau handshake kecuali perubahan diusulkan dan disetujui Agent Utama.

## Kontrak koordinasi Agent Windows

Agent Windows hanya mengubah file Windows dan dependency/build khusus Windows. Jangan mengubah `secure_transport.py`, handshake v3, protocol umum, file Mac, UI lintas platform atau nomor versi wire. Laporkan kebutuhan perubahan tersebut untuk disetujui dahulu.

**Input:** connected `socket`, authenticated `SecureChannel`, `send_message`, `recv_message`; sample stereo PCM dari WASAPI loopback dalam worker capture.

**Output:** `audio_format`, urutan `audio_frame`, serta `audio_state` melalui `channel.send(sock, message, send_message)`. Terima `audio_control` pada dispatcher yang sudah ada.

**Format:** JSON messages di atas; Opus 48 kHz stereo, 20 ms, payload Base64. `stream_id` UUID per sesi, `sequence` integer mulai 0, `timestamp_ns` monotonic. Maks ukuran message 12 KiB; jangan menaruh PCM mentah dalam JSON. Semua pesan audio harus tetap dienkripsi SecureChannel v3.

**Kompatibilitas:** jika peer belum mengirim `audio_control`/tidak mengenali pesan audio, mouse, keyboard, dan clipboard harus tetap normal. Audio nonaktif secara default sampai sesi audio diaktifkan. Satu sesi audio per peer; saat disconnect stop capture dan release COM/WASAPI resources.

**Uji/laporan:** jalankan pemeriksaan statis/unit yang relevan di Windows, uji WASAPI pada default output dengan musik stereo, toggle audio control, perangkat tidak tersedia, stop/start, disconnect/reconnect, antrean penuh, serta log tanpa data audio. Laporkan file berubah, versi Windows/Python, perangkat sample rate, codec binary/dependency, hasil uji aktual (pisahkan yang belum bisa dijalankan), latensi/underrun/drop dan risiko.

## Prompt implementasi Agent Windows

> Kerjakan bagian Windows Shared Audio pada source Mousehare yang sudah ada. Sebelum mengubah kode, baca `SHARED_AUDIO_DIAGNOSIS.md`, `mouseshare/windows/windows_app.py`, `mouseshare/windows/protocol.py`, `mouseshare/secure_transport.py`, dan build/dependency Windows. Implementasikan modul capture WASAPI loopback yang terpisah dari mouse/keyboard input engine. Gunakan kontrak v1 di diagnosis: WASAPI stereo → normalisasi 48 kHz stereo → Opus 20 ms → pesan `audio_format` dan `audio_frame` JSON Base64 lewat `SecureChannel.send` di TCP 5555; terima `audio_control` untuk start/stop. Audio off secara default. Callback capture hanya mengisi bounded buffer; encoding dan jaringan di worker. Tangani overflow, device invalidation, disconnect/reconnect, cleanup COM/WASAPI, status `audio_state`, dan jangan pernah memasukkan PCM mentah, pairing key, atau isi audio ke log. Jangan mengubah handshake, `secure_transport.py`, protokol umum, file Mac, UI bersama, atau semantik input. Jika implementasi memerlukan perubahan kontrak/dependency besar, berhenti pada catatan teknis dan ajukan proposal, jangan mengubah arsitektur sendiri. Tambahkan/ubah hanya kode Windows dan dependency/build Windows yang diperlukan. Uji pada Windows fisik bila tersedia; jangan menyatakan sukses untuk uji yang tidak dijalankan. Serahkan ringkasan file berubah, dependency/codec yang dipilih, cara build, hasil aktual tiap uji, sample rate/perangkat, metrik latensi/underrun/drop, keterbatasan, dan risiko.

## Roadmap

1. Diagnosis source selesai; konfirmasi batas minimum macOS dan buat native proof-of-concept process tap/output sebelum mengunci UI/kontrak dua sumber.
2. Windows capture + protokol frame audio v1; review kode dan dependency sebelum merge/integrasi.
3. Mac receiver/decode/mix untuk Windows-only, sehingga audio Windows bisa divalidasi terlebih dulu tanpa mengklaim Mac source gain.
4. Mac process tap + routing ulang, gain/mute Mac independen, output-device changes; baru setelahnya lengkapkan UI.
5. End-to-end test: Mac only, Windows only, both, independent gain/mute, disconnect/reconnect, output device change, long run. Catat fakta hasil uji; stress/timing dilakukan di hardware nyata.
6. Ukur latensi dan jitter. Jika TCP menimbulkan head-of-line delay atau merusak respons input, rancang transport UDP terautentikasi terpisah dengan koordinasi kedua platform.
7. **Build ulang wajib setelah integrasi/perbaikan:** build `.app` dan DMG macOS pada Mac lewat `scripts/build-macos.sh`; build folder aplikasi dan installer Windows lewat `scripts/build-windows.ps1` pada Windows. Pastikan binary native/codec dan metadata izin audio ikut terpaket. Buka hasil build dan verifikasi start, koneksi, audio, device change, serta sharing input sebelum menyatakan rilis siap.

## Risiko yang belum ditutup

- Proyek menyebut macOS 12+, sedangkan Core Audio taps memerlukan macOS 14.2+. Pilihan dukungan OS menjadi blocker arsitektur untuk slider Mac independen.
- Windows capture, WASAPI device recovery, native macOS tap/mixer, Opus packaging, signed/notarized driverless route belum diuji pada komputer nyata.
- TCP JSON/Base64 bukan media transport ideal; bitrate Opus moderat membuat prototipe masuk akal, tetapi tail latency harus diukur saat input/clipboard aktif.
- Perlu memastikan process/system tap tidak menangkap audio aplikasi Mousehare sendiri atau menimbulkan loop/echo saat Windows mix dimainkan kembali.

## Referensi Apple

- [Capturing system audio with Core Audio taps](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps)
- [CATapMuteBehavior](https://developer.apple.com/documentation/coreaudio/catapmutebehavior)
- [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
