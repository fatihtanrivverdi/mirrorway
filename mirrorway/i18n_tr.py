"""Turkish interface texts (English source text -> Turkish). Kept complete by tests/test_i18n.py."""

TR = {
    'main':
        'ana',
    'left':
        'solda',
    'right':
        'sağda',
    'above':
        'üstte',
    'below':
        'altta',
    'Monitor {n} — {w}×{h}':
        'Monitör {n} — {w}×{h}',
    'and {n} more':
        've {n} öğe daha',
    '{n} items, {size} ({names})':
        '{n} öğe, {size} ({names})',
    'too many items (at most {n})':
        'çok fazla öğe (en çok {n})',
    'could not read {path} ({error})':
        'okunamadı: {path} ({error})',
    'no files to send':
        'gönderilecek dosya yok',
    'cancelled':
        'iptal edildi',
    'a file got shorter while it was being sent: {path}':
        'dosya gönderilirken kısaldı: {path}',
    'the other side refused':
        'karşı taraf reddetti',
    'too many items':
        'çok fazla öğe',
    'too large ({size}; limit {limit})':
        'çok büyük ({size}; sınır {limit})',
    'invalid path: {path}':
        'geçersiz yol: {path}',
    'corrupt file data':
        'dosya verisi bozuk',
    'the transfer did not finish':
        'aktarım tamamlanmadı',
    'This Mirrorway server does not support the new connection format':
        'Bu Mirrorway sunucusu yeni bağlantı biçimini desteklemiyor',
    "This PC is not among the Mac's paired devices — pair again":
        "Bu bilgisayar Mac'te eşleşmiş cihazlar arasında yok — yeniden eşleştirin",
    'The Mac refused the connection (status {status})':
        'Mac bağlantıyı reddetti (durum {status})',
    "The Mac's identity is not the expected one ({id}); pair again":
        "Mac'in kimliği beklenenden farklı ({id}); yeniden eşleştirin",
    'The pairing code must have 6 digits':
        'Eşleştirme kodu 6 haneli olmalı',
    'This Mirrorway server does not support pairing':
        'Bu Mirrorway sunucusu eşleştirmeyi desteklemiyor',
    'Pairing is not open on the Mac: click Mirrorway › Pair a New Device…':
        "Mac'te eşleştirme açık değil: Mirrorway › Yeni cihaz eşleştir'e tıklayın",
    'Too many wrong attempts; get a new code on the Mac':
        "Çok fazla yanlış deneme; Mac'te yeni bir kod alın",
    'The Mac refused pairing (status {status})':
        'Mac eşleştirmeyi reddetti (durum {status})',
    'Wrong pairing code':
        'Eşleştirme kodu hatalı',
    '; weak recognition':
        '; zayıf tanıma',
    'Network restriction off (works on any network)':
        'Ağ kısıtlaması kapalı (her ağda çalışır)',
    'Home network ✓ (gateway {gateways}{weak})':
        'Ev ağı ✓ (ağ geçidi {gateways}{weak})',
    'Unknown network ({gateways}{weak}) — Mirrorway is off on this network':
        'Tanınmayan ağ ({gateways}{weak}) — Mirrorway bu ağda kapalı',
    'Network could not be determined':
        'Ağ belirlenemedi',
    'ScreenCaptureKit did not respond':
        'ScreenCaptureKit yanıt vermedi',
    'no Screen Recording permission, or the content could not be read: {error}':
        'Ekran Kaydı izni yok ya da içerik alınamadı: {error}',
    'audio capture could not start (timed out)':
        'ses yakalama başlatılamadı (zaman aşımı)',
    'audio capture could not start: {error}':
        'ses yakalama başlatılamadı: {error}',
    'Off':
        'Kapalı',
    'The virtual display closed unexpectedly; reopening it':
        'Sanal ekran beklenmedik biçimde kapandı; yeniden açılıyor',
    'Mirrorway on Windows does not support the second screen (needs an update)':
        "Windows'taki Mirrorway ikinci ekranı desteklemiyor (güncellenmeli)",
    'The selected Windows monitor was not found':
        'Seçilen Windows monitörü bulunamadı',
    'Unsupported monitor size: {w}×{h}':
        'Desteklenmeyen monitör boyutu: {w}×{h}',
    'Could not open the virtual display: {error}':
        'Sanal ekran açılamadı: {error}',
    'capture could not start (timed out)':
        'yakalama başlatılamadı (zaman aşımı)',
    'capture could not start: {error}':
        'yakalama başlatılamadı: {error}',
    'No screen capture method worked. Check the Screen Recording permission.':
        'Hiçbir ekran yakalama yöntemi çalışmadı. Ekran Kaydı iznini kontrol edin.',
    'No release has been published yet':
        'Henüz yayımlanmış bir sürüm yok',
    'GitHub is limiting requests; try again later':
        'GitHub istekleri sınırlıyor; biraz sonra tekrar deneyin',
    'GitHub replied with HTTP {code}':
        'GitHub yanıtı: HTTP {code}',
    'Could not reach GitHub: {reason}':
        "GitHub'a ulaşılamadı: {reason}",
    'There is no previous version to return to':
        'Dönülecek önceki sürüm yok',
    'Checking…':
        'Denetleniyor…',
    'New version: {version}':
        'Yeni sürüm: {version}',
    'Mirrorway is up to date ({version})':
        'Mirrorway güncel ({version})',
    'Could not check: {error}':
        'Denetlenemedi: {error}',
    'Downloading {version}…':
        '{version} indiriliyor…',
    'Installing {version}…':
        '{version} kuruluyor…',
    '{version} installed — restarting Mirrorway':
        '{version} kuruldu — Mirrorway yeniden başlatılıyor',
    'Could not install: {error}':
        'Kurulamadı: {error}',
    'One of the Windows monitors becomes a second screen for the Mac':
        "Windows monitörlerinden biri Mac'in ikinci ekranı olur",
    'Right':
        'Sağda',
    'Left':
        'Solda',
    'Above':
        'Üstte',
    'Below':
        'Altta',
    'Low (smooth, little bandwidth)':
        'Düşük (akıcı, az bant)',
    'Medium':
        'Orta',
    'High (sharp text)':
        'Yüksek (net yazı)',
    'H.264 — smooth (recommended)':
        'H.264 — akıcı (önerilen)',
    'JPEG — sharpest text':
        'JPEG — en keskin yazı',
    'Automatic':
        'Otomatik',
    'ISO (Turkish/European)':
        'ISO (Türkçe/Avrupa)',
    'ANSI (US)':
        'ANSI (ABD)',
    'Starting…':
        'Başlatılıyor…',
    'Measurements: —':
        'Ölçümler: —',
    'Pair a New Device…':
        'Yeni cihaz eşleştir…',
    'Network':
        'Ağ',
    'Network status':
        'Ağ durumu',
    'Trust This Network':
        'Bu ağa güven',
    'Work Only on the Home Network':
        'Yalnızca ev ağında çalış',
    'Reset Trusted Networks (keep only this one)':
        'Güvenilen ağları sıfırla (yalnızca bu ağ kalsın)',
    'Paired Devices':
        'Eşleşmiş cihazlar',
    'Second Screen (Windows monitor)':
        'İkinci ekran (Windows monitörü)',
    'This Mac':
        'Bu Mac',
    'Switch to Windows':
        "Windows'a geç",
    'Wake Windows':
        "Windows'u uyandır",
    'Windows Is on This Side of the Screen':
        'Windows ekranın hangi tarafında',
    'Pointer Speed (Windows)':
        'Fare hızı (Windows)',
    'Automatic Speed (screen ratio)':
        'Otomatik hız (ekran oranı)',
    'Scrolling':
        'Kaydırma',
    'Reverse Direction':
        'Yönü ters çevir',
    'Keyboard':
        'Klavye',
    'Left Option → AltGr (for @, € etc.)':
        'Sol Option → AltGr (@, € vb. için)',
    'Special Characters with Option (@ € …)':
        'Option ile özel karakterler (@ € …)',
    'Switch at the Screen Edge':
        'Ekran kenarından geçiş',
    'Clipboard Sync':
        'Pano senkronizasyonu',
    'Trackpad Gestures on Windows':
        "Trackpad hareketleri Windows'ta",
    'File Copy and Paste':
        'Dosya kopyala-yapıştır',
    'File transfer: —':
        'Dosya aktarımı: —',
    'Audio':
        'Ses aktarımı',
    "Play the Mac's Sound on Windows":
        "Mac'in sesi Windows'ta çalsın",
    "Mute the Mac's Speakers Meanwhile":
        "Bu sırada Mac'in hoparlörünü kapat",
    'Status: —':
        'Durum: —',
    'Screen Sharing':
        'Ekran paylaşımı',
    'Shared Display':
        'Paylaşılan ekran',
    'Picture Quality':
        'Görüntü kalitesi',
    'Adjust to the Connection Automatically (H.264)':
        'Bağlantıya göre otomatik ayarla (H.264)',
    'Video Format':
        'Görüntü biçimi',
    'Open at Login':
        'Mac açılınca başlat',
    'Language':
        'Dil',
    'Updates':
        'Güncelleme',
    'Check for Updates':
        'Güncellemeleri denetle',
    'No new version':
        'Yeni sürüm yok',
    'Check Automatically Once a Day':
        'Günde bir kez kendiliğinden denetle',
    'Return to the Previous Version':
        'Önceki sürüme dön',
    'Open Permission Settings':
        'İzin ayarlarını aç',
    'Status: off':
        'Durum: kapalı',
    'Status: playing on Windows':
        "Durum: Windows'ta çalıyor",
    'Status: available once Mirrorway on Windows is updated':
        "Durum: Windows'taki Mirrorway güncellenince kullanılabilir",
    'Status: starts when Windows connects':
        'Durum: Windows bağlanınca başlar',
    'Status: starting…':
        'Durum: başlatılıyor…',
    'File transfer: none yet':
        'Dosya aktarımı: henüz yok',
    'File transfer: to Windows, interrupted — {error}':
        "Dosya aktarımı: Windows'a, yarıda kaldı — {error}",
    'File transfer: from Windows, interrupted — {error}':
        "Dosya aktarımı: Windows'tan, yarıda kaldı — {error}",
    'Selected — starts when the screen window opens on Windows':
        "Seçili — Windows'ta ekran penceresi açılınca başlar",
    'Available once Mirrorway on Windows is updated':
        "Windows'taki Mirrorway güncellenince kullanılabilir",
    'If the computer turned on but nobody has signed in to Windows yet, Mirrorway connects after sign-in.':
        "Bilgisayar açıldı ama Windows'ta oturum açılmadıysa Mirrorway, oturum açılınca bağlanır.",
    'Wake Windows (Windows is on)':
        "Windows'u uyandır (Windows açık)",
    'Wake Windows (connect once first)':
        "Windows'u uyandır (önce bir kez bağlanın)",
    'the app you started Mirrorway from (e.g. Terminal)':
        "Mirrorway'i başlattığınız uygulamayı (ör. Terminal)",
    '⚠︎ Permission needed (Accessibility + Input Monitoring)':
        '⚠︎ İzin gerekli (Erişilebilirlik + Girdi İzleme)',
    '⏸ Unknown network — Mirrorway is off on this network (Mirrorway › Network)':
        '⏸ Tanınmayan ağ — Mirrorway bu ağda kapalı (Mirrorway › Ağ)',
    '(none)':
        '(yok)',
    'Available once Windows connects':
        'Windows bağlanınca seçilebilir',
    'Accessibility':
        'Erişilebilirlik',
    'Input Monitoring':
        'Girdi İzleme',
    'Screen Recording':
        'Ekran Kaydı',
    'Disconnect':
        'Bağlantıyı kes',
    'Show Log':
        'Günlüğü göster',
    'Create Diagnostics Bundle':
        'Tanı paketi oluştur',
    'Switch to Windows   ⌃⌥{arrow}   (back: ⌃⌥ + opposite arrow)':
        "Windows'a geç   ⌃⌥{arrow}   (dönüş: ⌃⌥ + ters ok)",
    'The new language is used after Mirrorway restarts. Restart now?':
        'Yeni dil Mirrorway yeniden başlayınca kullanılır. Şimdi yeniden başlatılsın mı?',
    "There is no new version to install. Try 'Check for Updates' first.":
        "Kurulacak yeni sürüm yok. Önce 'Güncellemeleri denetle'yi deneyin.",
    'Download and install Mirrorway {version}? Mirrorway restarts; Windows is updated too when it connects.':
        'Mirrorway {version} indirilip kurulsun mu? Mirrorway yeniden başlatılır; Windows bağlanınca o da güncellenir.',
    'There is no previous version to return to.':
        'Dönülecek önceki sürüm yok.',
    'The update is installed. Quit and reopen Mirrorway for it to take effect.':
        "Güncelleme kuruldu. Değişikliklerin geçerli olması için Mirrorway'i kapatıp yeniden açın.",
    'You can install it from Mirrorway › Updates.':
        'Mirrorway › Güncelleme menüsünden kurabilirsiniz.',
    'This network could not be recognised (no gateway found). Check the Wi-Fi or Ethernet connection.':
        'Bu ağ tanınamadı (ağ geçidi bulunamadı). Wi-Fi ya da Ethernet bağlantısını kontrol edin.',
    'Delete the list of trusted networks? The current network is saved as the home network again.':
        'Güvenilen ağ listesi silinsin mi? Şu anki ağ yeniden ev ağı olarak kaydedilir.',
    'Paired Devices ({n})':
        'Eşleşmiş cihazlar ({n})',
    'Click a device to remove its pairing':
        'Bir cihaza tıklayınca eşleşmesi kaldırılır',
    'File transfer: to Windows {percent}% ({done} / {total})':
        "Dosya aktarımı: Windows'a %{percent} ({done} / {total})",
    'File transfer: from Windows {percent}% ({done} / {total})':
        "Dosya aktarımı: Windows'tan %{percent} ({done} / {total})",
    'File transfer: to Windows, done — {summary}':
        "Dosya aktarımı: Windows'a tamam — {summary}",
    'File transfer: from Windows, done — {summary}':
        "Dosya aktarımı: Windows'tan tamam — {summary}",
    'Windows is not connected. Start Mirrorway on Windows and enter the pairing code.':
        "Windows bağlı değil. Windows'ta Mirrorway'i çalıştırıp eşleştirme kodunu girin.",
    'Windows is already on and connected.':
        'Windows zaten açık ve bağlı.',
    'The address of the Windows network adapter is not known yet.\n\nIt is saved the first time Windows connects to Mirrorway; after that you can wake it from this menu.':
        "Windows'un ağ kartı adresi henüz bilinmiyor.\n\nWindows Mirrorway'e bir kez bağlandığında adres kaydedilir; sonra bu menüden uyandırabilirsiniz.",
    'Windows did not wake up (Windows settings may be needed)':
        'Windows uyanmadı (Windows ayarları gerekebilir)',
    'Wake Windows ({name})':
        "Windows'u uyandır ({name})",
    "In System Settings > Privacy & Security, turn on {who} in both the 'Accessibility' and the 'Input Monitoring' list. If it is not listed, add it with the + button. After granting permission you may need to quit and reopen Mirrorway.":
        "Sistem Ayarları > Gizlilik ve Güvenlik altında {who} hem 'Erişilebilirlik' hem 'Girdi İzleme' listesinde açın. Listede yoksa + düğmesiyle ekleyin. İzin verdikten sonra Mirrorway'den çıkıp yeniden açmanız gerekebilir.",
    'Measurements: {text}':
        'Ölçümler: {text}',
    '○ Waiting for Windows…':
        '○ Windows bekleniyor…',
    'VPN is on: Windows may not be able to connect':
        'VPN açık: Windows bağlanamayabilir',
    'Network: home network ✓':
        'Ağ: ev ağı ✓',
    'Network: restriction off':
        'Ağ: kısıtlama kapalı',
    'This Mac: {address}{extra}  (copy)':
        'Bu Mac: {address}{extra}  (kopyala)',
    'Display {n} ({w}×{h})':
        'Ekran {n} ({w}×{h})',
    'Automatic (system)':
        'Otomatik (sistem)',
    'Restart':
        'Yeniden başlat',
    'Later':
        'Sonra',
    'Status: ⚠︎ {error}':
        'Durum: ⚠︎ {error}',
    "Open Mirrorway's settings on Windows and enter this code:\n\n{code}\n\nThe code is valid for {time} minutes and works once. The device appears in the menu once pairing is complete.":
        "Windows'ta Mirrorway ayarlarını açıp şu kodu girin:\n\n{code}\n\nKod {time} dakika geçerli ve tek kullanımlıktır. Eşleşme tamamlanınca menüde görünür.",
    'Install and Restart':
        'Kur ve yeniden başlat',
    'Cancel':
        'Vazgeç',
    'Return and Restart':
        'Dön ve yeniden başlat',
    'Install {version} and Restart':
        '{version} sürümünü kur ve yeniden başlat',
    'Return to the Previous Version ({version})':
        'Önceki sürüme dön ({version})',
    'Update Available: {version} ●':
        'Güncelleme var: {version} ●',
    'Reset':
        'Sıfırla',
    'No paired devices':
        'Eşleşmiş cihaz yok',
    'connected':
        'bağlı',
    'verifying':
        'doğrulanıyor',
    'On — a {w}×{h} virtual display on the Mac':
        "Açık — Mac'te {w}×{h} sanal ekran",
    '(the monitors appear once Windows connects)':
        '(Windows bağlanınca monitörler görünür)',
    'Remove':
        'Kaldır',
    "On Windows, in Device Manager > Network adapters > the adapter > Power Management, 'Allow this device to wake the computer' and 'Only allow a magic packet…' must be on":
        "Windows'ta Aygıt Yöneticisi > Ağ bağdaştırıcıları > kart > Güç Yönetimi: 'Bu aygıtın bilgisayarı uyandırmasına izin ver' ve 'Yalnızca sihirli paket…' açık olmalı",
    "On the Advanced tab, 'Wake on Magic Packet' must be Enabled":
        "Gelişmiş sekmesinde 'Wake on Magic Packet' Etkin olmalı",
    "If it was shut down completely: turn off 'Fast startup' in Control Panel > Power Options and enable Wake-on-LAN in the BIOS":
        "Tamamen kapatıldıysa: Denetim Masası > Güç Seçenekleri'nde 'Hızlı başlatmayı aç' kapatılmalı ve BIOS'ta Wake-on-LAN açık olmalı",
    'Waking Windows… ({seconds} s)':
        'Windows uyandırılıyor… ({seconds} sn)',
    'Windows woke up ✓ ({seconds} s)':
        'Windows uyandı ✓ ({seconds} sn)',
    'Mirrorway (or the old Köprü) is already running (the Mirrorway icon in the menu bar).\n\nOn MacBooks with a full menu bar the icon may hide behind the camera notch.\n\nQuit the running one and start this one? (Needed after granting permissions.)':
        "Mirrorway (ya da eski Köprü) zaten çalışıyor (menü çubuğundaki Mirrorway simgesi).\n\nMacBook'larda menü çubuğu doluysa simge kameranın (çentiğin) arkasında kalabilir.\n\nÇalışan kapatılıp bu başlatılsın mı? (İzin verdikten sonra gereklidir.)",
    'waiting…':
        'bekleniyor…',
    '● You are controlling {name}{version}':
        "● {name}{version} — Windows'u kontrol ediyorsunuz",
    '● {name}{version} connected':
        '● {name}{version} bağlı',
    'Network: unknown network ⏸':
        'Ağ: tanınmayan ağ ⏸',
    'Network: undetermined':
        'Ağ: belirlenemedi',
    'Pairing code: {code}  ({time})':
        'Eşleştirme kodu: {code}  ({time})',
    'Automatic speed: {scale}× screen ratio (total {total}×)':
        'Otomatik hız: {scale}× ekran oranı (toplam {total}×)',
    'Speed {n}×':
        'Hız {n}×',
    'Keyboard Type: {name}':
        'Klavye tipi: {name}',
    'Quit Mirrorway {version}':
        'Mirrorway {version} — Çıkış',
    "What's new:":
        'Yenilikler:',
    'Return to the previous version ({version})? Mirrorway restarts.':
        'Önceki sürüme ({version}) dönülsün mü? Mirrorway yeniden başlatılır.',
    "Remove the pairing with '{name}' ({id})?\n\nThis device will no longer be able to connect; to use it again, pair it with a new code.":
        "'{name}' ({id}) eşleşmesi kaldırılsın mı?\n\nBu cihaz bir daha bağlanamaz; yeniden kullanmak için yeni bir kodla eşleştirmeniz gerekir.",
    'latency {ms} ms':
        'gecikme {ms} ms',
    'software':
        'yazılım',
    'screen {codec} {fps} fps, {mbs} MB/s, encoding {ms} ms':
        'ekran {codec} {fps} fps, {mbs} MB/s, kodlama {ms} ms',
    '(weak recognition)':
        '(zayıf tanıma)',
    "'{name}' is paired (fingerprint {id}).":
        "'{name}' eşleştirildi (parmak izi {id}).",
    'Could not change the setting: {error}':
        'Ayarlanamadı: {error}',
    'Could not return: {error}':
        'Dönülemedi: {error}',
    'Mirrorway {version} is ready':
        'Mirrorway {version} hazır',
    'Could not create the diagnostics bundle: {error}':
        'Tanı paketi oluşturulamadı: {error}',
    'picture latency ≈ {ms} ms':
        'görüntü gecikmesi ≈ {ms} ms',
    'Permission is needed to send the keyboard and mouse to Windows.':
        "Klavye ve fareyi Windows'a aktarabilmek için izin gerekiyor.",
    'Windows did not connect to Mirrorway within {seconds} seconds ({mac}, {ip}).':
        "Windows {seconds} saniye içinde Mirrorway'e bağlanmadı ({mac}, {ip}).",
    'Could not open port {port}: {error}':
        'Port {port} açılamadı: {error}',
    'Port {port} is in use by another app.':
        'Port {port} başka bir uygulama tarafından kullanılıyor.',
    'Port {port} is still in use: {error}':
        'Port {port} hâlâ kullanımda: {error}',
    'audio is turned off on the Mac':
        "Mac'te ses aktarımı kapalı",
    'cannot capture sound: {error}':
        'ses yakalanamıyor: {error}',
    '{ip} tried to pair: pairing is not open on the Mac':
        "{ip} eşleştirme denedi: Mac'te eşleştirme açık değil",
    '{ip} entered a wrong pairing code':
        '{ip} yanlış eşleştirme kodu girdi',
    '{ip} tried to connect with an unpaired device':
        '{ip} eşleşmemiş bir cihazla bağlanmaya çalıştı',
    '{ip} tried to connect with a wrong pairing code':
        '{ip} yanlış eşleştirme koduyla bağlanmaya çalıştı',
    '{ip} tried to connect with the old pairing code (now disabled)':
        '{ip} eski eşleştirme koduyla bağlanmaya çalıştı (artık kapalı)',
    'screen sharing is turned off on the Mac':
        "Mac'te ekran paylaşımı kapalı",
    'Files were not sent to Windows':
        "Dosyalar Windows'a gönderilmedi",
    'Files were not sent':
        'Dosyalar gönderilmedi',
    '{summary} — limit {limit}':
        '{summary} — sınır {limit}',
    'this copy is no longer current':
        'bu kopya artık geçerli değil',
    'Windows could not receive the files':
        'Windows dosyaları alamadı',
    'file copy and paste is turned off on the Mac':
        "Mac'te dosya kopyala-yapıştır kapalı",
    'larger than the limit on the Mac':
        "Mac'teki sınırdan büyük",
    'Files arrived from Windows':
        "Windows'tan dosyalar geldi",
    '{summary}. Paste them in Finder with ⌘V.':
        "{summary}. Finder'da ⌘V ile yapıştırabilirsin.",
    'Windows is connected over Wi-Fi; Wake-on-LAN does not work with most Wi-Fi adapters (an Ethernet cable is recommended)':
        'Windows Wi-Fi ile bağlı; Wake-on-LAN çoğu Wi-Fi kartında çalışmaz (Ethernet kablosu önerilir)',
    "Device Manager > network adapter > Power Management: 'Only allow a magic packet to wake the computer' is off":
        "Aygıt Yöneticisi > ağ kartı > Güç Yönetimi'nde 'Yalnızca sihirli paket bilgisayarı uyandırabilir' kapalı",
    "Network adapter > Advanced tab: 'Wake on Magic Packet' is off":
        "Ağ kartı > Gelişmiş sekmesinde 'Wake on Magic Packet' kapalı",
    "The adapter's 'Allow this device to wake the computer' setting is off":
        "Ağ kartının 'Bu aygıtın bilgisayarı uyandırmasına izin ver' ayarı kapalı",
    'Fast startup is on; waking may not work after a shutdown (it works from sleep)':
        'Hızlı başlatma açık; kapatıldıktan sonra uyandırma çalışmayabilir (uyku modunda çalışır)',
    'target {mbit} Mbit/s':
        'hedef {mbit} Mbit/s',
    'auto':
        'otomatik',
    'JPEG (changed regions)':
        'JPEG (değişen bölgeler)',
    'latency ≈ {lat} ms · network {rtt} ms':
        'gecikme ≈ {lat} ms · ağ {rtt} ms',
    'decode {ms}':
        'çözme {ms}',
    'Mac {enc} · {decode} · draw {disp} ms':
        'Mac {enc} · {decode} · çizim {disp} ms',
    'file transfer needs a device key':
        'dosya aktarımı için cihaz anahtarı gerekli',
    'Receiving files from the Mac':
        "Mac'ten dosyalar alınıyor",
    'Files from the Mac are ready':
        "Mac'ten dosyalar hazır",
    '{summary}. Paste them in Explorer with Ctrl+V.':
        "{summary}. Explorer'da Ctrl+V ile yapıştırabilirsin.",
    'Files were not sent to the Mac':
        "Dosyalar Mac'e gönderilmedi",
    'Sending files to the Mac':
        "Dosyalar Mac'e gönderiliyor",
    'the Mac could not receive the files':
        'Mac dosyaları alamadı',
    'Could not send the files to the Mac':
        "Dosyalar Mac'e gönderilemedi",
    'Pairing required: on the Mac click Mirrorway › Pair a New Device… and enter the code':
        "Eşleştirme gerekli: Mac'te Mirrorway › Yeni cihaz eşleştir'e tıklayıp kodu girin",
    'Looking for the Mac…':
        'Mac aranıyor…',
    'Connecting to {address}…':
        '{address} adresine bağlanılıyor…',
    'This PC is not paired with the Mac — get a code on the Mac with Mirrorway › Pair a New Device… and enter it here':
        "Bu bilgisayar Mac'te eşleşmiş değil — Mac'te Mirrorway › Yeni cihaz eşleştir ile kodu alıp buraya girin",
    'The old pairing code is no longer valid — pair again on the Mac with Mirrorway › Pair a New Device…':
        "Eski eşleştirme kodu artık geçerli değil — Mac'te Mirrorway › Yeni cihaz eşleştir ile yeniden eşleştirin",
    'Could not connect ({host}): {error}':
        'Bağlanamadı ({host}): {error}',
    'Connected to {name}':
        '{name} ile bağlı',
    'Connection dropped, retrying…':
        'Bağlantı koptu, yeniden deneniyor…',
    'Looking for the Mac to pair with…':
        'Eşleştirme için Mac aranıyor…',
    'Pairing with {name}…':
        '{name} ile eşleştiriliyor…',
    'Could not pair: {error}':
        'Eşleştirilemedi: {error}',
    'Could not open the pairing connection ({host}): {error}':
        'Eşleştirme bağlantısı kurulamadı ({host}): {error}',
    'Paired with {name}, connecting…':
        '{name} ile eşleştirildi, bağlanılıyor…',
    'Updating to {version}…':
        '{version} sürümüne güncelleniyor…',
    'Connected to {name} (update failed)':
        '{name} ile bağlı (güncelleme başarısız)',
    'Pairing required (on the Mac: Mirrorway › Pair a New Device…)':
        "Eşleştirme gerekli (Mac'te Mirrorway › Yeni cihaz eşleştir)",
    'Waiting for the screen of {name}…':
        '{name} ekranı bekleniyor…',
    'Authentication failed: {error}':
        'Kimlik doğrulanamadı: {error}',
    'No video connection: {error}':
        'Görüntü bağlantısı yok: {error}',
    'Connecting…':
        'Bağlanılıyor…',
    'latency ≈ {ms} ms':
        'gecikme ≈ {ms} ms',
    '(F11: full screen, F9: measurements)':
        '(F11: tam ekran, F9: ölçümler)',
    "the Mac's keyboard and mouse are on this PC":
        'Mac klavye/faresi bu bilgisayarda',
    'Paired with {name}.':
        '{name} ile eşleştirildi.',
    "Show the Mac's Screen":
        'Mac ekranını göster',
    'Settings…':
        'Ayarlar…',
    'Reconnect':
        'Yeniden bağlan',
    'Open the Log Folder':
        'Günlük klasörünü aç',
    'Quit (Mirrorway {version})':
        'Çıkış (Mirrorway {version})',
    'The new language is used after Mirrorway restarts.':
        'Yeni dil Mirrorway yeniden başlayınca kullanılır.',
    'Mac screen':
        'Mac ekranı',
    'Updated to Mirrorway {version}.':
        'Mirrorway {version} sürümüne güncellendi.',
    'Settings':
        'Ayarlar',
    'Pairing code:':
        'Eşleştirme kodu:',
    'Mac address:':
        'Mac adresi:',
    "Leave it empty to find the Mac automatically. If it is not found, enter the 'This Mac: …' address from the Mac menu.":
        "Boş bırakırsanız Mac otomatik bulunur. Bulunamazsa Mac menüsündeki 'Bu Mac: …' adresini yazın.",
    'Remove Pairing':
        'Eşleşmeyi kaldır',
    'Save and Connect':
        'Kaydet ve bağlan',
    'Close':
        'Kapat',
    'Paired Mac: {name} (fingerprint {id})':
        'Eşleşilen Mac: {name} (parmak izi {id})',
    'This PC connects to the Mac with its device key. To pair with another Mac, click Mirrorway › Pair a New Device… on that Mac and enter the code below.':
        "Bu bilgisayar Mac'e cihaz anahtarıyla bağlanıyor. Başka bir Mac ile eşleştirmek için Mac'te Mirrorway › Yeni cihaz eşleştir'e tıklayıp kodu aşağıya girin.",
    'New pairing code:':
        'Yeni eşleştirme kodu:',
    "On the Mac, click the Mirrorway icon in the menu bar, choose 'Pair a New Device…' and enter the 6-digit code it shows below.":
        "Mac'te menü çubuğundaki Mirrorway simgesine tıklayın, 'Yeni cihaz eşleştir'i seçin ve görünen 6 haneli kodu aşağıya girin.",
    "Remove this PC's pairing with the Mac? Connecting again will need a new code from the Mac.":
        "Bu bilgisayarın Mac ile eşleşmesi kaldırılsın mı? Yeniden bağlanmak için Mac'ten yeni bir kod gerekir.",
    'Mac found on the network: {list}':
        'Ağda bulunan Mac: {list}',
    'Discovery is not working (UDP 24851 could not be opened); enter the address by hand.':
        'Otomatik bulma çalışmıyor (UDP 24851 açılamadı); adresi elle yazın.',
    'Looking for the Mac on the network…':
        'Ağda Mac aranıyor…',
}
