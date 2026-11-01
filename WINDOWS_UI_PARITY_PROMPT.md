# Prompt untuk agent Windows

Kerjakan di branch `production-v4`. Selaraskan tampilan Windows Mousehare dengan macOS pada `mouseshare_app.py`: palet TVA gelap, panel terpisah, sidebar Connect/Settings/About, status bar, jarak dan hierarki yang sama, serta teks gelap berkontras tinggi di tombol amber. Saat tombol difokuskan atau diklik, garis/outline fokus harus tetap amber sesuai tema, jangan berubah menjadi putih.

Tambahkan ikon mata di sisi kanan field IP, Port, dan Pairing Key. IP dan port terlihat pada awal; pairing key tersembunyi. Toggle harus dapat dipakai dengan mouse dan keyboard tanpa mengubah nilai input. Terapkan juga pada field terkait di Settings. Pastikan outline fokus pada tombol maupun panel berbingkai tidak berubah putih saat diklik atau saat salah satu field di dalam panel mendapat fokus. Set warna fokus frame secara eksplisit agar konsisten dengan border bronze/amber; hilangkan outline native putih/putus-putus yang tersisa tanpa menghilangkan akses keyboard.

Tinjau Connect, semua bagian Settings, About, status/error/disabled, serta jendela pada ukuran normal dan minimum. Pertahankan konten khusus Windows dan jangan mengubah protokol, pairing, keamanan, atau perilaku handoff.

Build di Windows dengan `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1`, buka hasilnya, lalu laporkan pemeriksaan visual, hasil build, dan masalah tersisa. Jangan push perubahan.
