# Prompt Implementasi Agent Windows — Mousehare Shared Audio

Kerjakan bagian Windows Shared Audio pada source Mousehare yang sudah ada. Sebelum mengubah kode, baca `SHARED_AUDIO_DIAGNOSIS.md`, `mouseshare/windows/windows_app.py`, `mouseshare/windows/protocol.py`, `mouseshare/secure_transport.py`, dan build/dependency Windows.

Implementasikan modul capture WASAPI loopback yang terpisah dari mouse/keyboard input engine. Gunakan kontrak v1 di diagnosis:

- WASAPI stereo → normalisasi 48 kHz stereo → Opus 20 ms.
- Kirim pesan `audio_format` dan `audio_frame` JSON Base64 melalui `SecureChannel.send` pada koneksi TCP Mousehare port 5555.
- Terima `audio_control` untuk start/stop capture.
- Audio nonaktif secara default.
- Callback capture hanya mengisi bounded buffer; encoding dan jaringan berjalan di worker.
- Tangani overflow, device invalidation, disconnect/reconnect, cleanup COM/WASAPI, dan status `audio_state`.
- Jangan memasukkan PCM mentah, pairing key, atau isi audio ke log.

Jangan mengubah handshake, `secure_transport.py`, protokol umum, file Mac, UI bersama, atau semantik input. Jika implementasi memerlukan perubahan kontrak/dependency besar, berhenti pada catatan teknis dan ajukan proposal; jangan mengubah arsitektur sendiri.

Tambahkan/ubah hanya kode Windows dan dependency/build Windows yang diperlukan. Uji pada Windows fisik bila tersedia; jangan menyatakan sukses untuk pengujian yang tidak dijalankan.

## Format komunikasi

- Pesan audio tetap melewati secure channel v3 yang sudah ada.
- `audio_format`: `{type:"audio_format", version:1, codec:"opus", sample_rate:48000, channels:2, frame_ms:20}`.
- `audio_frame`: `{type:"audio_frame", stream_id, sequence, timestamp_ns, payload:<base64 Opus>}`.
- `stream_id` adalah UUID per sesi; `sequence` integer mulai dari 0 dan naik monotonik; `timestamp_ns` memakai clock monotonic.
- Ukuran pesan maksimal 12 KiB. Jangan mengirim PCM mentah di JSON.
- Kontrol dari Mac: `{type:"audio_control", enabled:boolean}`.
- Jika peer belum memakai audio atau tidak mengenali pesan audio, mouse, keyboard, dan clipboard harus tetap bekerja normal.
- Satu sesi audio per peer. Saat disconnect, hentikan capture dan lepaskan semua resource COM/WASAPI.

## Pengujian dan laporan hasil

Jalankan pemeriksaan statis/unit yang relevan di Windows, lalu uji WASAPI pada default output dengan musik stereo, toggle audio control, perangkat tidak tersedia, stop/start, disconnect/reconnect, antrean penuh, dan logging tanpa data audio.

Laporkan:

1. File yang diubah.
2. Versi Windows dan Python serta perangkat/sample rate yang digunakan.
3. Codec/binary/dependency dan perubahan packaging yang diperlukan.
4. Hasil aktual setiap pengujian, pisahkan yang tidak dapat dijalankan.
5. Metrik latensi, underrun, dan packet/drop.
6. Keterbatasan dan risiko yang ditemukan.
