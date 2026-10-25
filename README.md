# Mousehare (MouseShare v2)

> **Solusi Berbagi Keyboard & Mouse Antar-OS (macOS Client ⇄ Windows Server) melalui Jaringan Lokal Terenkripsi**

Mousehare adalah aplikasi desktop yang menghubungkan satu keyboard dan mouse dari komputer **Mac** untuk mengendalikan komputer **Windows** secara *seamless* melalui jaringan lokal (LAN) berlatensi rendah. Aplikasi ini dilengkapi dengan antarmuka grafis desktop modern (Tkinter retro-TVA theme), tray icon di menu bar / taskbar, penyimpanan pengaturan otomatis, serta transmisi terenkripsi berstandar industri.

---

## Daftar Isi

1. [Tentang Mousehare & Arsitektur Sistem](#1-tentang-mousehare--arsitektur-sistem)
   - [Peran OS: Mengapa Mac Client & Windows Server?](#peran-os-mengapa-mac-client--windows-server)
   - [Arsitektur Aliran Data (Data Flow)](#arsitektur-aliran-data-data-flow)
   - [Protokol Keamanan & Jaringan](#protokol-keamanan--jaringan)
2. [Prasyarat Lingkungan (Environment Prerequisites)](#2-prasyarat-lingkungan-environment-prerequisites)
   - [Spesifikasi macOS](#spesifikasi-macos)
   - [Spesifikasi Windows](#spesifikasi-windows)
3. [Panduan Setup & Menjalankan Mode Development](#3-panduan-setup--menjalankan-mode-development)
   - [Clone Repositori](#clone-repositori)
   - [Setup Lingkungan macOS](#setup-lingkungan-macos)
   - [Setup Lingkungan Windows](#setup-lingkungan-windows)
   - [Menjalankan Aplikasi (Development Mode)](#menjalankan-aplikasi-development-mode)
   - [Menjalankan Agent Mandiri (Standalone Agent)](#menjalankan-agent-mandiri-standalone-agent)
4. [Panduan Build (Kompilasi Binary & Installer)](#4-panduan-build-kompilasi-binary--installer)
   - [Build Deliverable di macOS (.app & .dmg)](#build-deliverable-di-macos-app--dmg)
   - [Build Deliverable di Windows (.exe Installer via Inno Setup)](#build-deliverable-di-windows-exe-installer-via-inno-setup)
5. [Panduan Instalasi Pengguna Akhir (End-User Installation)](#5-panduan-instalasi-pengguna-akhir-end-user-installation)
   - [Instalasi di macOS](#instalasi-di-macos)
   - [Konfigurasi Izin macOS (Accessibility & Input Monitoring)](#konfigurasi-izin-macos-accessibility--input-monitoring)
   - [Instalasi di Windows (10 / 11)](#instalasi-di-windows-10--11)
   - [Konfigurasi Windows Firewall & SmartScreen](#konfigurasi-windows-firewall--smartscreen)
6. [Panduan Konfigurasi & Pengoperasian](#6-panduan-konfigurasi--pengoperasian)
   - [Langkah Pairing Antar-Komputer](#langkah-pairing-antar-komputer)
   - [Konfigurasi Posisi Monitor Fisik](#konfigurasi-posisi-monitor-fisik)
   - [Indikator Status (System Tray & Menu Bar)](#indikator-status-system-tray--menu-bar)
   - [File Konfigurasi & Lokasi Data](#file-konfigurasi--lokasi-data)
7. [Troubleshooting & Tanya Jawab (FAQ)](#7-troubleshooting--tanya-jawab-faq)
8. [Catatan Roadmap & Rencana Fitur Masa Depan (Future Versions)](#8-catatan-roadmap--rencana-fitur-masa-depan-future-versions)
   - [V.3: Fitur Copy-Paste Antar OS (MVP)](#v3-fitur-copy-paste-antar-os-mvp)
   - [V.4: Sinkronisasi Koordinat Proporsional Persentase Layar & Visibilitas Kursor (Hide/Show Bilateral)](#v4-sinkronisasi-koordinat-proporsional-persentase-layar--visibilitas-kursor-hideshow-bilateral)
   - [V.5: Perbaikan Style Visual & Ilustrasi Layout Interaktif Dinamis](#v5-perbaikan-style-visual--ilustrasi-layout-interaktif-dinamis)
   - [V.5: Fitur Pengaturan Shortcut / Hotkey Kustom](#v5-fitur-pengaturan-shortcut--hotkey-kustom)

---

## 1. Tentang Mousehare & Arsitektur Sistem

### Peran OS: Mengapa Mac Client & Windows Server?

Pada arsitektur Mousehare v2:
- **macOS bertindak sebagai CLIENT (Input Provider / Controller)**.
- **Windows bertindak sebagai SERVER (Input Consumer / Receiver)**.

Arsitektur ini ditentukan secara sengaja berdasarkan karakteristik sistem operasi:
1. **macOS**: Memiliki subsistem Quartz Core Graphics yang menyediakan `CGEventTap`. Mekanisme ini memungkinkan penangkapan (*hook*) seluruh event mouse dan keyboard secara global di level sistem, menahannya (*suppression* agar tidak mengklik aplikasi di Mac), dan menyalurkannya keluar melalui koneksi TCP.
2. **Windows**: Berperan sebagai penerima yang menginjeksi input mouse dan keyboard ke sistem menggunakan Win32 User32 APIs (`SetCursorPos`, `mouse_event`, `SendInput`) dan library `pynput`, serta memantau pergerakan kursor di tepi layar yang menghadap Mac untuk mengembalikan kontrol (*return edge detection*).

```
 ┌───────────────────────────┐                 ┌───────────────────────────┐
 │       macOS CLIENT        │                 │      Windows SERVER       │
 │  (Keyboard & Mouse Fisik) │                 │      (Host Penerima)      │
 ├───────────────────────────┤                 ├───────────────────────────┤
 │ • Quartz CGEventTap       │                 │ • TCP Socket Server:5555  │
 │ • Cursor Dissociation     │  TCP Enkripsi   │ • Win32 Input Injection   │
 │ • Event Suppression       │ ──────────────> │ • pynput Controller       │
 │ • Edge Handoff Triggers   │   (AES-GCM)     │ • Edge Return Listener    │
 └───────────────────────────┘                 └───────────────────────────┘
```

### Arsitektur Aliran Data (Data Flow)

1. **MAC_ACTIVE State**: Pengguna menggerakkan mouse dan mengetik di Mac. Event input mengalir normal ke aplikasi-aplikasi di Mac.
2. **Edge Crossing**: Ketika kursor mouse Mac mencapai batas tepi layar yang mengarah ke Windows (misalnya tepi kanan layar Mac jika Mac diposisikan di sebelah kiri Windows), Mac memicu transisi `switch_to_windows()`:
   - Mac mengirim paket kontrol `{"type": "activate_windows", "position": <fraction>}`.
   - Mac melepaskan kaitan mouse fisik dari kursor visual (`CGAssociateMouseAndMouseCursorPosition(False)`).
   - Kursor Mac disembunyikan (`CGDisplayHideCursor()`) dan diparkir di tepi agar tidak berkeliaran di Mac.
   - Status beralih ke `WINDOWS_ACTIVE`.
3. **WINDOWS_ACTIVE State**: Setiap pergerakan mouse delta (`dx`, `dy`), klik tombol, *scroll wheel*, dan ketukan tombol keyboard ditahan dari Mac dan dikirimkan secara berurutan (*ordered coalesced queue*) ke Windows. Windows langsung menginjeksi input tersebut sehingga pengguna mengendalikan Windows secara langsung.
4. **Return Crossing**: Di Windows, `pynput` listener memantau batas layar yang menghadap Mac. Ketika kursor di Windows menyentuh tepi batas tersebut, Windows mengirim paket `{"type": "edge", "edge": <edge>, "position": <fraction>}` dan melepaskan status aktif.
5. **Kembali ke Mac**: Mac menerima sinyal *edge*, memulihkan asosiasi kursor (`CGAssociateMouseAndMouseCursorPosition(True)`), menampilkan kursor kembali (`CGDisplayShowCursor()`), menempatkannya di tepi layar yang sesuai, dan mengembalikan status ke `MAC_ACTIVE`.

### Protokol Keamanan & Jaringan

- **Transport**: TCP berbasis *framed protocol* dengan header 4-byte *big-endian length prefix* diikuti payload JSON UTF-8.
- **Port Default**: `5555` (dapat dikustomisasi via Settings).
- **Handshake & Pertukaran Kunci**: Menggunakan kunci ephemeral kurva eliptik **X25519**. Kunci rahasia bersama (*shared secret*) diturunkan menggunakan **HKDF-SHA256**.
- **Enkripsi Sesi**: Semua payload data kontrol dan input dienkripsi menggunakan **AES-GCM (128-bit / 256-bit)** dengan *nonce* dan *monotonically increasing sequence number* untuk mencegah serangan *replay*, manipulasi data, atau pembajakan sesi di jaringan lokal.
- **Pairing Key (Token)**: Kunci pairing acak sepanjang 22 karakter URL-safe (128-bit entropy) di-*generate* di Windows dan dimasukkan pada Mac. Kunci ini diverifikasi melalui mekanisme *HMAC challenge-response* tanpa pernah dikirimkan dalam bentuk teks mentah melalui jaringan.

---

## 2. Prasyarat Lingkungan (Environment Prerequisites)

Pastikan lingkungan komputer memenuhi spesifikasi berikut sebelum memulai:

### Spesifikasi macOS
- **Versi OS**: macOS 12 Monterey, macOS 13 Ventura, macOS 14 Sonoma, macOS 15 Sequoia (atau lebih baru).
- **Arsitektur**: Apple Silicon (M1/M2/M3/M4, `arm64`) atau Intel (`x86_64`).
- **Python**: Python **3.13** (disarankan) atau minimal Python 3.10+ dengan dukungan modul **Tcl/Tk** (Tkinter).
  > **Catatan Tkinter di Mac**: Gunakan installer resmi dari [python.org](https://www.python.org/downloads/) karena sudah menyertakan binary Tkinter yang stabil, atau jika menggunakan Homebrew, install modul Tkinter via `brew install python-tk@3.13`.
- **Xcode Command Line Tools**: Diperlukan untuk utilitas sistem (`iconutil`, `PlistBuddy`, `codesign`). Install dengan perintah:
  ```bash
  xcode-select --install
  ```

### Spesifikasi Windows
- **Versi OS**: Windows 10 atau Windows 11 (64-bit / x64).
- **Python**: Python **3.12** (disarankan) atau minimal Python 3.10+ dari [python.org](https://www.python.org/downloads/) atau Microsoft Store.
  > **PENTING saat instalasi Python di Windows**: Centang opsi **"Add python.exe to PATH"** dan pastikan opsi **"tcl/tk and IDLE"** tetap tercentang.
- **Inno Setup 6**: *(Wajib hanya jika ingin mem-build installer Windows `.exe`)*. Unduh installer gratis dari [jrsoftware.org](https://jrsoftware.org/isdl.php) dan pasang di direktori default (`C:\Program Files (x86)\Inno Setup 6` atau `C:\Program Files\Inno Setup 6`).
- **PowerShell**: PowerShell 5.1 bawaan Windows atau PowerShell 7+.

---

## 3. Panduan Setup & Menjalankan Mode Development

### Clone Repositori

Buka terminal pada komputer (Mac maupun Windows):

```bash
git clone https://github.com/zackisb/mouseshare.git
cd "mouseshare"
```
*(Ganti URL git sesuai dengan URL repositori Anda jika di-fork).*

---

### Setup Lingkungan macOS

1. **Buka Terminal di folder proyek** dan buat Python Virtual Environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. **Upgrade pip dan install dependensi development**:
   ```bash
   pip install --upgrade pip setuptools wheel
   pip install -r requirements-macos.txt
   ```
   *Dependensi inti macOS mencakup: `pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, `pystray`, `Pillow`, `cryptography`, `cffi`, `PyInstaller`.*

---

### Setup Lingkungan Windows

1. **Buka Command Prompt (cmd) atau PowerShell di folder proyek** dan buat Virtual Environment:
   ```powershell
   py -3.12 -m venv .venv
   ```
   *(Atau `python -m venv .venv` jika menggunakan python default).*

2. **Install dependensi development**:
   > **Catatan PowerShell**: Untuk menghindari masalah *Script Execution Policy*, Anda dapat mengeksekusi pip langsung melalui file executable python di dalam virtual environment:
   ```powershell
   .venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
   .venv\Scripts\python.exe -m pip install -r requirements-windows.txt
   ```
   *Dependensi inti Windows mencakup: `pynput`, `pystray`, `Pillow`, `cryptography`, `cffi`, `PyInstaller`.*

---

### Menjalankan Aplikasi (Development Mode)

Aplikasi utama `mouseshare_app.py` secara otomatis mendeteksi platform OS:
- Di **macOS**: Membuka dashboard client Mousehare.
- Di **Windows**: Membuka dashboard server Mousehare.

#### Di macOS:
```bash
source .venv/bin/activate
python mouseshare_app.py
```
> [!IMPORTANT]
> Saat dijalankan pertama kali melalui Terminal, macOS akan meminta izin **Accessibility** dan **Input Monitoring** untuk aplikasi **Terminal** (atau iTerm2/VS Code). Izinkan kedua hak akses tersebut di *System Settings → Privacy & Security*.

#### Di Windows:
```powershell
.venv\Scripts\python.exe mouseshare_app.py
```
Aplikasi akan menampilkan jendela kontrol panel Mousehare dengan IP lokal Windows yang terdeteksi otomatis.

---

### Menjalankan Agent Mandiri (Standalone Agent)

Selain GUI terpadu, masing-masing agent di folder `mouseshare/mac/` dan `mouseshare/windows/` dapat dijalankan secara mandiri menggunakan konfigurasi berbasis file `config.json`:

- **Di macOS (Client Standalone)**:
  ```bash
  cd mouseshare/mac
  python mac_app.py
  ```
  *(Membaca konfigurasi dari `mouseshare/mac/config.json`)*.

- **Di Windows (Server Standalone)**:
  ```powershell
  cd mouseshare\windows
  ..\..\.venv\Scripts\python.exe windows_app.py
  ```
  *(Membaca konfigurasi dari `mouseshare/windows/config.json`)*.

---

## 4. Panduan Build (Kompilasi Binary & Installer)

Build dilakukan pada masing-masing sistem operasi target agar binary native (`.app` / `.exe`) dan pustaka runtime OS ter-bundle dengan sempurna.

### Build Deliverable di macOS (.app & .dmg)

Skrip otomatis [scripts/build-macos.sh](file:///Users/zackisaefulbahri/Pemograman/MouseShare%20v2/scripts/build-macos.sh) menangani seluruh proses pembuatan bundle aplikasi:

1. Membuat environment terisolasi di `.build/venv-macos`.
2. Menghasilkan icon aplikasi macOS beresolusi tinggi (`.build/icons/mouseshare.icns`) melalui `scripts/make_icons.py`.
3. Menjalankan PyInstaller dengan mode `--windowed --onedir` untuk membuat bundle `Mousehare.app`.
4. Menginjeksikan deskripsi string izin `NSAccessibilityUsageDescription` dan `NSInputMonitoringUsageDescription` ke dalam `Info.plist` menggunakan utilitas `PlistBuddy`.
5. Melakukan penandatanganan kode (*code-signing*):
   - **Ad-hoc Signing (Default lokal)**: Digunakan jika variabel `MACOS_CODESIGN_IDENTITY` tidak diisi.
   - **Developer ID Signing**: Jika memiliki sertifikat Apple Developer ID resmi:
     ```bash
     MACOS_CODESIGN_IDENTITY="Developer ID Application: Nama Anda (TEAM_ID)" bash scripts/build-macos.sh
     ```
6. Mengemas aplikasi ke dalam format Apple Disk Image (`.dmg`) menggunakan `hdiutil` lengkap dengan shortcut ke `/Applications`.

**Cara Eksekusi:**
```bash
bash scripts/build-macos.sh
```

**Output Hasil Build:**
- Bundle Aplikasi: `dist/Mousehare.app`
- File Installer DMG: `release/Mousehare-macos-arm64.dmg` (untuk Apple Silicon) atau `release/Mousehare-macos-x86_64.dmg` (untuk Intel).

---

### Build Deliverable di Windows (.exe Installer via Inno Setup)

Skrip otomatis [scripts/build-windows.ps1](file:///Users/zackisaefulbahri/Pemograman/MouseShare%20v2/scripts/build-windows.ps1) menangani pembuatan executable dan setup wizard di Windows:

1. Membuat virtual environment terisolasi di `.build\venv-windows`.
2. Menghasilkan icon aplikasi Windows (`.build\icons\mouseshare.ico`).
3. Menjalankan PyInstaller `--windowed --onedir` untuk membuat folder aplikasi `dist\Mousehare\`.
4. Mencari letak compiler Inno Setup (`ISCC.exe`) secara otomatis di direktori Program Files.
5. Mengompilasi naskah instalasi [packaging/windows/Mousehare.iss](file:///Users/zackisaefulbahri/Pemograman/MouseShare%20v2/packaging/windows/Mousehare.iss).

**Cara Eksekusi (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

**Output Hasil Build:**
- Folder Binary: `dist\Mousehare\`
- Installer Wizard: `release\Mousehare-Setup-1.0.0.exe`

Installer ini menggunakan konfigurasi `PrivilegesRequired=lowest` (per-user installation), sehingga pengguna dapat menginstalnya tanpa memerlukan hak administrator sistem, membuat entri pada Start Menu, dan opsi shortcut desktop.

---

## 5. Panduan Instalasi Pengguna Akhir (End-User Installation)

### Instalasi di macOS

1. Unduh atau salin file `Mousehare-macos-arm64.dmg` (atau versi x86_64).
2. Klik ganda file `.dmg` untuk membukanya.
3. Tarik ikon **Mousehare.app** ke folder **Applications**.
4. Buka **Applications** dan jalankan **Mousehare**.

> [!NOTE]
> Jika muncul peringatan keamanan macOS Gatekeeper (*"Mousehare cannot be opened because the developer cannot be verified"*):
> 1. Buka **System Settings → Privacy & Security**.
> 2. Gulir ke bawah ke bagian *Security*, lalu klik **Open Anyway** pada Mousehare.
> 3. Atau melalui Terminal: `xattr -cr /Applications/Mousehare.app`.

### Konfigurasi Izin macOS (Accessibility & Input Monitoring)

Agar mouse dan keyboard fisik dapat ditangkap dan dialihkan ke Windows, macOS mewajibkan otorisasi keamanan privasi (TCC):

1. Buka **System Settings → Privacy & Security → Accessibility**.
   - Klik tombol toggle untuk mengaktifkan **Mousehare**.
2. Buka **System Settings → Privacy & Security → Input Monitoring**.
   - Klik tombol toggle untuk mengaktifkan **Mousehare**.
3. Jika aplikasi meminta konfirmasi *Quit & Reopen*, pilih **Quit & Reopen**.

```
┌────────────────────────────────────────────────────────┐
│ macOS System Settings → Privacy & Security             │
├────────────────────────────────────────────────────────┤
│  [✓] Accessibility      ──> [ Aktifkan Mousehare ]     │
│  [✓] Input Monitoring   ──> [ Aktifkan Mousehare ]     │
└────────────────────────────────────────────────────────┘
```

> [!TIP]
> **Penting Mengenai Rebuild Ad-hoc**: Jika Anda mengompilasi ulang aplikasi dengan signature ad-hoc (`-`), macOS menganggap binary baru tersebut memiliki identitas kode yang berbeda. Jika status izin tampak aktif tetapi aplikasi melaporkan *Permission Denied*, hapus entri Mousehare lama dengan menekan tombol minus `(-)` di pengaturan Accessibility & Input Monitoring, kemudian tambahkan kembali file `/Applications/Mousehare.app`.

---

### Instalasi di Windows (10 / 11)

1. Jalankan file `Mousehare-Setup-1.0.0.exe`.
2. Ikuti panduan wizard instalasi (centang opsi *Create a desktop shortcut* jika diinginkan).
3. Klik **Finish** untuk menyelesaikan instalasi dan membuka aplikasi.
4. Aplikasi akan terpasang di direktori profil pengguna (misal: `%LOCALAPPDATA%\Programs\Mousehare` atau `{autopf}`).

### Konfigurasi Windows Firewall & SmartScreen

- **Windows SmartScreen**: Jika muncul jendela biru *"Windows protected your PC"*, klik tulisan **More info**, kemudian klik tombol **Run anyway**.
- **Windows Defender Firewall**: Saat pertama kali menjalankan Server, Windows akan menampilkan peringatan keamanan firewall. Centang opsi **Private networks (Jaringan Privat / Rumah / Kantor)** dan klik **Allow access**.
  *(Port default yang digunakan adalah TCP 5555).*

> [!NOTE]
> **Batasan Privilese Administrator (UAC)**:
> Jika Mousehare di Windows dijalankan sebagai pengguna standar (non-admin), kursor mouse tidak akan dapat mengklik jendela aplikasi yang dijalankan dengan hak akses Administrator (misal Task Manager, Registry Editor, atau Command Prompt yang di-Run as Administrator) karena proteksi UIPI (*User Interface Privilege Isolation*) Windows. Jika Anda sering mengelola aplikasi administratif, jalankan Mousehare di Windows dengan cara klik kanan shortcut → **Run as administrator**.

---

## 6. Panduan Konfigurasi & Pengoperasian

Pastikan kedua komputer terhubung pada **jaringan Wi-Fi atau kabel LAN yang sama**.

### Langkah Pairing Antar-Komputer

```
┌─────────────────────────────────┐           ┌─────────────────────────────────┐
│         KOMPUTER WINDOWS        │           │           KOMPUTER MAC          │
│             (SERVER)            │           │             (CLIENT)            │
├─────────────────────────────────┤           ├─────────────────────────────────┤
│ 1. Buka Mousehare di Windows    │           │                                 │
│ 2. Klik "Start Server"          │           │                                 │
│ 3. Catat Info yang Tertera:     │           │ 4. Buka Mousehare di Mac        │
│    • Local IP  : 192.168.1.50   │ ────────> │ 5. Masukkan IP Windows          │
│    • Port      : 5555           │           │ 6. Masukkan Port (5555)         │
│    • Key Token : xYz123... (22) │           │ 7. Masukkan Key Token           │
│                                 │           │ 8. Pilih Posisi Mac (mis: Left) │
│                                 │ <──────── │ 9. Klik "Connect"               │
│ [Status: Connected & Paired]    │           │ [Status: Connected (Green)]     │
└─────────────────────────────────┘           └─────────────────────────────────┘
```

1. **Di Komputer Windows (Server)**:
   - Buka Mousehare. Halaman utama menampilkan kartu peran *Windows Host (Server)*.
   - Klik tombol **Start Server**.
   - Salin **IP Address LAN** yang ditampilkan (misalnya `192.168.1.50`), nilai **Port** (`5555`), dan **Pairing Key**.
2. **Di Komputer Mac (Client)**:
   - Buka Mousehare. Halaman utama menampilkan kartu peran *Mac Controller (Client)*.
   - Masukkan IP Windows ke kolom **Windows Host / IP**.
   - Masukkan nomor port ke kolom **Port**.
   - Masukkan kode pairing ke kolom **Pairing Key**.
   - Tentukan letak posisi Mac terhadap Windows pada dropdown **Mac Position** (lihat sub-bab di bawah).
   - Klik tombol **Connect**.
3. **Koneksi Terjalin**:
   - Status bar di kedua aplikasi akan berubah menjadi **Connected** dengan indikator warna hijau.
   - Kunci pairing dan alamat IP disimpan secara otomatis, sehingga Anda tidak perlu mengetiknya kembali saat aplikasi dibuka di kemudian hari.

---

### Konfigurasi Posisi Monitor Fisik

Pengaturan **Mac Position** menentukan tepi layar mana yang digunakan untuk berpindah kontrol:

| Pengaturan Mac Position | Penjelasan Letak Fisik | Tepi Keluar di Mac | Tepi Masuk di Windows | Tepi Kembali ke Mac |
| :--- | :--- | :--- | :--- | :--- |
| **`left`** *(Default)* | Mac berada di **sebelah kiri** monitor Windows | Tepi **Kanan** Mac | Tepi **Kiri** Windows | Kursor ke tepi kiri Windows |
| **`right`** | Mac berada di **sebelah kanan** monitor Windows | Tepi **Kiri** Mac | Tepi **Kanan** Windows | Kursor ke tepi kanan Windows |
| **`top`** | Mac berada di **atas** monitor Windows | Tepi **Bawah** Mac | Tepi **Atas** Windows | Kursor ke tepi atas Windows |
| **`bottom`** | Mac berada di **bawah** monitor Windows | Tepi **Atas** Mac | Tepi **Bawah** Windows | Kursor ke tepi bawah Windows |

---

### Indikator Status (System Tray & Menu Bar)

Ikon lingkaran pada Menu Bar (macOS) dan System Tray (Windows) memberikan informasi visual status saat ini:

| Warna Ikon | Keterangan Status | Makna Perilaku |
| :--- | :--- | :--- |
| ⚪ **Abu-abu (Gray)** | *Disconnected* | Komputer belum terhubung atau server belum dinyalakan. |
| 🟢 **Hijau (Green)** | *Connected — Mac Active* | Komputer terhubung aman. Keyboard dan mouse saat ini mengendalikan **Mac**. |
| 🔵 **Biru (Blue)** | *Connected — Windows Active* | Komputer terhubung aman. Input keyboard dan mouse saat ini sedang disalurkan untuk mengendalikan **Windows**. |

---

### File Konfigurasi & Lokasi Data

Mousehare menyimpan pengaturan per-user secara aman pada direktori sistem standar:
- **macOS**: `~/Library/Application Support/Mousehare/settings.json` (diberi proteksi izin file `0700` khusus user).
- **Windows**: `%APPDATA%\Mousehare\settings.json` (biasanya berada di `C:\Users\<User>\AppData\Roaming\Mousehare\settings.json`).
- **File Log Diagnostik**:
  - macOS: `~/Library/Application Support/Mousehare/Mousehare.log`
  - Windows: `%APPDATA%\Mousehare\Mousehare.log`

---

## 7. Troubleshooting & Tanya Jawab (FAQ)

### Q1: Mengapa izin Accessibility & Input Monitoring hanya diminta di Mac, bukan di Windows?
> **Jawaban**: Di macOS, Apple menerapkan arsitektur keamanan TCC (*Transparency, Consent, and Control*) yang sangat ketat. Program tidak diizinkan menyadap (*listen*) maupun menyuntikkan (*post*) event mouse dan keyboard tingkat global tanpa persetujuan eksplisit dari pengguna. Karena Mac bertindak sebagai penyadap input utama (*client controller*), izin `Accessibility` dan `Input Monitoring` mutlak diperlukan. Sebaliknya, Windows bertindak sebagai penerima (*consumer*), di mana Windows API memperbolehkan injeksi input ke aplikasi desktop normal tanpa prompt dialog privasi khusus (hanya memerlukan izin jaringan Firewall).

### Q2: Izin sudah dicentang di Mac, tapi aplikasi tetap menolak atau muncul status "Denied"?
> **Jawaban**: Ini biasanya terjadi karena perbedaan identitas *code signature* setelah aplikasi di-build ulang (ad-hoc signing) atau adanya perbedaan path eksekusi.
> **Solusi**:
> 1. Keluar dari Mousehare.
> 2. Buka **System Settings → Privacy & Security → Accessibility**.
> 3. Pilih **Mousehare**, lalu klik tombol **[-] (Hapus)** untuk membersihkan cache izin lama.
> 4. Lakukan hal yang sama pada **Input Monitoring**.
> 5. Buka kembali `/Applications/Mousehare.app` dan aktifkan kembali tombol toggle izin yang diminta.

### Q3: Mengapa kursor mouse tidak bisa mengklik aplikasi tertentu di Windows (misalnya Task Manager)?
> **Jawaban**: Windows melindungi aplikasi dengan integritas tinggi melalui fitur UIPI (*User Interface Privilege Isolation*). Aplikasi non-admin tidak dapat mengirim pesan sintetis ke aplikasi yang berjalan sebagai Administrator.
> **Solusi**: Tutup Mousehare di Windows, lalu jalankan kembali dengan cara **Klik Kanan icon Mousehare → Run as administrator**.

### Q4: Muncul pesan error "Connection Refused" atau "Handshake Failed"?
> **Jawaban**:
> 1. Pastikan kedua komputer berada pada segmen Wi-Fi/LAN yang sama (bukan jaringan *Guest* atau jaringan dengan isolasi AP/AP Client Isolation).
> 2. Pastikan port `5555` tidak diblokir oleh Windows Firewall atau antivirus pihak ketiga.
> 3. Periksa kembali **Pairing Key**: pastikan token yang dimasukkan pada Mac sama persis dengan yang ada pada Windows (panjang 22 karakter, peka huruf besar/kecil).
> 4. Jika menggunakan Windows Server versi lama tanpa enkripsi, aktifkan opsi *"Allow a legacy Windows server without encryption"* di menu **Settings → Security** pada Mac.

---

## 8. Catatan Roadmap & Rencana Fitur Masa Depan (Future Versions)

Berikut adalah daftar rencana pembaruan dan peningkatan fitur yang telah didokumentasikan untuk versi-versi mendatang:

### V.3: Fitur Copy-Paste Antar OS (MVP)
*Target: Sinkronisasi Papan Klip Dua Arah (macOS ⇄ Windows)*

- **Deskripsi Fitur**:
  Pengguna dapat melakukan copy teks di Mac (`Cmd+C`) dan langsung menempelkannya di Windows (`Ctrl+V`), begitu pula sebaliknya dari Windows ke Mac secara otomatis dan transparan.
- **Rencana Teknis Implementasi**:
  1. **Fondasi Kode**: Modul [mouseshare/clipboard_sync.py](file:///Users/zackisaefulbahri/Pemograman/MouseShare%20v2/mouseshare/clipboard_sync.py) yang telah dirancang akan diaktifkan secara default sebagai mode sinkronisasi teks UTF-8 aman (maksimum 48 KiB).
  2. **Pencegahan Echo-Loop**: Mengimplementasikan pelacakan hash teks dan penanda kepemilikan paket (*timestamp/sequence*) agar teks yang baru saja diterima dari remote tidak dikirimkan kembali ke komputer asal secara berulang-ulang.
  3. **Event-Driven Listener**: Meningkatkan metode pemantauan clipboard dari *polling timer* periodik (`0.25s`) menjadi native OS clipboard event listener:
     - Di Windows: Memanfaatkan Win32 message listener `AddClipboardFormatListener` / `WM_CLIPBOARDUPDATE`.
     - Di macOS: Memanfaatkan polling efisien berbasis `NSPasteboard.generalPasteboard().changeCount` yang sangat hemat CPU.
  4. **Keamanan Transmisi**: Data clipboard dikirimkan di dalam saluran terenkripsi AES-GCM yang sudah terotentikasi, dengan opsi pengaturan untuk mematikan fitur clipboard jika pengguna menangani data sensitif.

---

### V.4: Sinkronisasi Koordinat Proporsional Persentase Layar & Visibilitas Kursor (Hide/Show Bilateral)
*Target: Preservasi Posisi Vertikal/Horizontal Proporsional Berbasis Persentase, Eliminasi Drift, dan Kursor Menghilang Otomatis pada Komputer Non-Aktif*

- **Latar Belakang & Masalah**:
  1. **Masalah Kursor Masih Muncul / Tidak Hilang Bilateral**:
     - Saat kontrol berpindah ke Windows, kursor visual di layar Mac terkadang masih tampak membeku atau melayang di tepi layar Mac dan tidak hilang sepenuhnya.
     - Sebaliknya, saat kontrol kembali ke Mac, kursor di Windows tetap tertinggal membeku di layar Windows. Pengguna mengharapkan pengalaman alami seperti multi-monitor fisik: kursor mouse visual hanya boleh tampak di SATU layar komputer yang sedang aktif dikendalikan. Ketika mouse nyebrang ke Windows, kursor di Mac harus hilang; begitu juga saat kembali ke Mac, kursor di Windows harus hilang/tidak terlihat.
  2. **Masalah Perbedaan Resolusi Layar & Rasio Aspek (Y-Axis Misalignment)**:
     - Layar Mac Retina (misal 2560x1600 atau 1440p) dan layar Windows (misal 1920x1080) memiliki jumlah piksel vertikal yang berbeda. Tanpa normalisasi persentase, kursor yang keluar di tepi kanan Mac pada posisi tertentu akan melompat ke titik yang tidak sejajar secara ketinggian fisik di Windows.
  3. **Masalah Pergeseran Kursor di Mac (*Mouse Drift*)**:
     - Kursor di Mac harus benar-benar dikunci posisinya (*frozen*) dan dilepaskan kaitan fisiknya secara instan pada event tap pertama agar tidak ikut bergeser secara fisik ke sudut lain saat pengguna menggerakkan mouse di Windows.

- **Rencana Teknis Implementasi**:
  1. **Manajemen Visibilitas Kursor Bilateral (Hide / Show Otomatis)**:
     - **Di Mac saat Beralih ke Windows (`switch_to_windows`)**:
       - Memastikan panggilan `_hide_cursor()` (`CGDisplayHideCursor(CGMainDisplayID())`) dieksekusi secara instan dan seimbang dengan call stack.
       - Parkir koordinat kursor Mac di koordinat tepi yang aman atau batas virtual non-interaktif saat mouse di-*dissociate* (`CGAssociateMouseAndMouseCursorPosition(False)`), mencegah kursor Mac ter-trigger unhide oleh event hover aplikasi Cocoa lain.
     - **Di Windows saat Kontrol Kembali ke Mac (`_handle_deactivate_windows`)**:
       - Pada arsitektur Windows User32, kursor tidak otomatis hilang secara global tanpa manipulasi. Solusi yang akan diimplementasikan:
         - **Metode A (Off-screen Parking)**: Menggeser posisi kursor Windows ke area koordinat di luar layar tampak (`user32.SetCursorPos`) saat status `STATE.active = False`, sehingga kursor Windows tidak terlihat membeku di tengah layar.
         - **Metode B (Blank Transparent System Cursor)**: Mengganti sementara kursor standar sistem Windows dengan kursor kosong transparan via `SetSystemCursor(hBlankCursor, OCR_NORMAL)` saat Mac aktif, dan mengembalikannya ke kursor standar sistem saat Windows aktif kembali (`_handle_activate_windows`).
  2. **Kalkulasi Persentase Relatif Proporsional (0.0 s/d 1.0)**:
     - Saat mouse menyentuh tepi batas keluar di Mac (misal tepi kanan), hitung posisi fraksi proporsional sepanjang sumbu vertikal (Y):
       $$\text{fraction}_y = \frac{y_{\text{mac}} - \text{top}_{\text{mac}}}{\max(1, \text{height}_{\text{mac}} - 1)}$$
       Nilai fraksi proporsional ini (misalnya `0.90` jika mouse berada di 90% ketinggian layar Mac) dikirimkan dalam paket `activate_windows`.
     - Windows menerima nilai `fraction` tersebut dan menempatkan kursor pada koordinat absolut yang proporsional terhadap resolusi vertikal Windows:
       $$y_{\text{win}} = \text{top}_{\text{win}} + \text{round}\left(\text{fraction}_y \times \max(0, \text{height}_{\text{win}} - 1)\right)$$
       *(Hasilnya: Kursor yang keluar di ketinggian 90% layar Mac akan masuk tepat pada ketinggian 90% layar Windows).*
  3. **Penanganan Sebaliknya (Windows kembali ke Mac)**:
     - Saat mouse di Windows menyentuh tepi yang menghadap Mac (misal tepi kiri), Windows menghitung fraksi proporsional ketinggian Y kursornya:
       $$\text{fraction}_{y,\text{win}} = \frac{y_{\text{win}} - \text{top}_{\text{win}}}{\max(1, \text{height}_{\text{win}} - 1)}$$
     - Paket `edge` mengirimkan fraksi tersebut ke Mac.
     - Mac menempatkan kembali kursornya di Mac pada persentase Y yang sama persis via `CGWarpMouseCursorPosition`, lalu memanggil `_show_cursor()` dan `_associate_mouse()`.
  4. **Penguncian Total Kursor Mac (*Zero-Leak Suppression*)**:
     - Menjamin flag `_switching_to_win` langsung menahan seluruh event mouse pada Quartz tap callback tanpa jeda thread asinkron, memastikan kursor di Mac tidak berpindah posisi sama sekali selama pengguna aktif bekerja di layar Windows.

---

### V.5: Perbaikan Style Visual & Ilustrasi Layout Interaktif Dinamis
*Target: Ilustrasi Monitor Reaktif & Real-Time pada Canvas Dashboard*

- **Latar Belakang & Masalah**:
  Pada menu Connect dan Settings di `mouseshare_app.py`, terdapat ilustrasi visual canvas (`_draw_link_visual` / `_paint_link`) yang menggambarkan diagram monitor Mac dan Windows. Namun saat ini gambar tersebut belum sepenuhnya merespons secara instan ketika pengguna mengganti pilihan dropdown posisi Mac (Left/Right/Top/Bottom), serta belum tersinkronisasi visualnya secara dua arah antara client dan server.
- **Rencana Teknis Implementasi**:
  1. **Reactive Redraw Binding**:
     Mengaitkan variabel Tkinter `mac_position` dengan trigger otomatis yang memanggil `_paint_link()` setiap kali nilai pengaturan posisi diubah, tanpa perlu merefresh atau membuka ulang halaman aplikasi.
  2. **Representasi Visual Monitor Modern**:
     - Menggambar ilustrasi monitor Mac (ikon gaya iMac/MacBook dengan warna tema) dan monitor PC Windows dengan proporsional.
     - Posisi monitor di canvas akan berpindah secara dinamis: jika Mac diatur di `Left`, monitor Mac berada di kiri; jika diatur di `Right`, monitor Mac bergeser ke kanan; begitu juga untuk `Top` dan `Bottom`.
     - Menambahkan indikator garis panah bercahaya (*accent glow*) yang menunjukkan arah transmisi kursor yang sedang aktif.
  3. **Sinkronisasi Tampilan ke Server**:
     Saat Mac terhubung ke Windows dengan orientasi tertentu, server Windows akan menerima informasi konfigurasi tata letak tersebut dan memperbarui canvas di jendela aplikasi Windows agar ilustrasi di kedua layar seragam.

---

### V.5: Fitur Pengaturan Shortcut / Hotkey Kustom
*Target: Pintasan Keyboard untuk Pergantian Layar Instan & Kontrol Darurat*

- **Latar Belakang & Kebutuhan**:
  Dalam skenario tertentu (seperti bermain game, bekerja di aplikasi desain grafis fullscreen, atau saat kursor terhambat di tepi layar), pengguna membutuhkan cara cepat untuk berpindah layar tanpa harus menggeser mouse ke ujung tepi, atau untuk merebut kembali kontrol mouse ke Mac jika terjadi gangguan koneksi.
- **Rencana Teknis Implementasi**:
  1. **Halaman Pengaturan Baru (Settings → Shortcuts)**:
     Menambahkan tab *Shortcuts* di antarmuka grafis yang memungkinkan pengguna merekam kombinasi tombol kustom (misalnya `Ctrl + Alt + S` atau `Cmd + Option + W`).
  2. **Fitur-fitur Shortcut yang Direncanakan**:
     - **Toggle / Quick Switch**: Tombol pintas untuk langsung memindahkan kontrol aktif antara Mac dan Windows secara instan.
     - **Emergency Release / Return**: Tombol pintas darurat (misal `Cmd + Shift + Escape`) yang seketika mematikan sesi remote, memulihkan kursor Mac, dan menampilkan kembali kursor fisik di layar Mac saat Windows mengalami *freeze* atau lag jaringan.
     - **Screen Lock (Cursor Trap)**: Tombol pintas untuk mengunci kursor di layar saat ini agar tidak berpindah ke komputer lain secara tidak sengaja saat memainkan game atau menggeser scrollbar di tepi layar.
  3. **Global Keystroke Handling**:
     Mengintegrasikan deteksi hotkey global di event tap Mac (`mac_app.py`) dan keyboard listener Windows (`windows_app.py`) sehingga kombinasi tombol tersebut dikenali secara instan di latar belakang.

---

## Ringkasan Struktur Direktori Proyek

```
MouseShare v2/
├── README.md                      # Dokumentasi master proyek (file ini)
├── RELEASE.md                     # Catatan teknis rilis, signing & verifikasi
├── Mousehare.spec                 # Konfigurasi bundling PyInstaller
├── requirements-macos.txt         # Pinned requirements build macOS (Python 3.13)
├── requirements-windows.txt       # Pinned requirements build Windows (Python 3.12)
├── mouseshare_app.py              # Aplikasi desktop utama (GUI Tkinter terpadu)
├── mouseshare/
│   ├── secure_transport.py        # Handshake X25519, HKDF, dan enkripsi AES-GCM
│   ├── clipboard_sync.py          # Modul sinkronisasi clipboard dua arah
│   ├── input_coalescer.py         # Buffer antrean input berurutan & peredam beban delta
│   ├── mac/
│   │   ├── mac_app.py             # Agent macOS: Quartz event tap & cursor handling
│   │   ├── protocol.py            # Framed TCP helper (header 4-byte + JSON)
│   │   ├── config.json            # Konfigurasi default agent macOS
│   │   ├── requirements.txt       # Dependensi minimal agent macOS
│   │   └── README.md              # Dokumentasi teknis agent macOS
│   └── windows/
│       ├── windows_app.py         # Agent Windows: TCP server & pynput injection
│       ├── protocol.py            # Framed TCP helper (header 4-byte + JSON)
│       ├── config.json            # Konfigurasi default agent Windows
│       ├── requirements.txt       # Dependensi minimal agent Windows
│       └── README.md              # Dokumentasi teknis agent Windows
├── packaging/
│   └── windows/
│       └── Mousehare.iss          # Script naskah kompilasi Inno Setup 6
└── scripts/
    ├── build-macos.sh             # Script otomatis kompilasi macOS .app & .dmg
    ├── build-windows.ps1          # Script otomatis kompilasi Windows .exe installer
    └── make_icons.py              # Script pembuat asset icon (.icns & .ico)
```

---

## Lisensi & Kontribusi

Mousehare dikembangkan sebagai utilitas input-sharing sumber terbuka berlatensi rendah untuk kenyamanan kerja multi-komputer. Kontribusi perbaikan bug, penambahan fitur pada roadmap, dan optimalisasi kinerja dipersilakan melalui mekanisme *Pull Request*.
