/* ============================================================
   Ksenos Messenger — the window's behaviour
   PIN gate, conversation history and the chat itself.
   ============================================================ */

(() => {
  "use strict";

  const API = "/messenger/api";
  const SPRITE = "/mini-app/static/assets";

  const STRINGS = {
    uz: {
      "app.title": "Ksenos Messenger",
      "gate.title": "Kirish kodi",
      "gate.sub": "Akkauntingizga kirish uchun 4 xonali kodni kiriting",
      "gate.dotsAria": "PIN kod",
      "gate.account": "Akkaunt yaratish",
      "gate.noAccount": "Akkaunt hali yaratilmagan. Pastdagi tugma bilan boshlang.",
      "gate.locked": "Kod {time} dan keyin qayta ochiladi",
      "gate.already": "Akkaunt allaqachon yaratilgan: bitta akkaunt, bitta doimiy kod.",
      "gate.setupTitle": "PIN kod yarating",
      "gate.setupSub": "Bu 4 xonali kod doimiy: bir marta oʻrnatiladi va hech qachon oʻzgarmaydi",
      "gate.repeatTitle": "Kodni takrorlang",
      "gate.repeatSub": "Xatolik boʻlmasligi uchun kodni yana kiriting",
      "gate.wrong": "Kod notoʻgʻri. Qaytadan urinib koʻring.",
      "gate.mismatch": "Kodlar mos kelmadi. Boshidan boshlang.",
      "gate.tooMany": "Juda koʻp urinish. Bir daqiqadan keyin qayta urinib koʻring.",
      "gate.offline": "Server javob bermayapti. main.py ishlab turganini tekshiring.",
      "gate.saved": "Kod saqlandi. Ochilmoqda…",
      "rail.lock": "Qulflash",
      "rail.new": "Yangi suhbat",
      "rail.search": "Tarixdan qidirish…",
      "rail.history": "Yozishmalar tarixi",
      "rail.empty": "Hali suhbat yoʻq. Birinchi xabarni yozing — tarix shu yerda saqlanadi.",
      "rail.noMatch": "Bu soʻz bilan suhbat topilmadi.",
      "chat.menu": "Menyu",
      "chat.online": "online · faqat agent",
      "chat.emptyTitle": "Suhbatni boshlang",
      "chat.emptyText": "Kontakt kerak emas: yozing, javob shu yerda boʻladi. Tarix avtomatik saqlanadi.",
      "chat.typing": "Ksenos yozmoqda…",
      "chat.placeholder": "Xabar yozing…",
      "chat.send": "Yuborish",
      "chat.failed": "Javob kelmadi. Qayta urinib koʻring.",
      "day.today": "Bugun",
      "day.yesterday": "Kecha",
      "toast.deleted": "Suhbat oʻchirildi.",
      "toast.deleteFailed": "Suhbatni oʻchirib boʻlmadi.",
      "toast.locked": "Qulflandi.",
      "meta.messages": "{n} xabar",
      "chat.settings": "Sozlamalar",
      "set.title": "Sozlamalar",
      "set.close": "Yopish",
      "set.profile": "Profil",
      "set.upload": "Rasm yuklash",
      "set.remove": "Oʻchirish",
      "set.name": "Ism",
      "set.namePlace": "Ismingiz",
      "set.appearance": "Koʻrinish — 6 dizayn",
      "set.language": "Til",
      "set.pin": "Kirish kodi",
      "set.pinShow": "Koʻrsatish",
      "set.pinHide": "Yashirish",
      "set.pinNote": "Kod doimiy: bir marta oʻrnatiladi va hech qachon oʻzgarmaydi.",
      "set.pinLock": "{n} marta xato → {m} daqiqa blok.",
      "set.ai": "AI va kalitlar",
      "set.emoji": "Javoblarda emoji ishlatilsin",
      "set.apiKey": "Oʻz API kalitingiz",
      "set.apiKeyPlace": "Kalitni kiriting",
      "set.keyAdd": "Kalit qoʻshish",
      "set.keyNote": "Kalit tugasa, ilova avtomatik keyingisiga oʻtadi. Kalitlar shu kompyuterda saqlanadi.",
      "set.keys": "Zaxira kalitlar: {n}",
      "set.about": "Windows uchun shaxsiy AI chat. Maʼlumotlar shu kompyuterda qoladi.",
      "theme.obsidian": "Obsidian",
      "theme.midnight": "Midnight",
      "theme.emerald": "Emerald",
      "theme.sepia": "Sepia",
      "theme.violet": "Violet",
      "theme.graphite": "Graphite",
      "toast.saved": "Saqlandi.",
      "toast.failed": "Saqlab boʻlmadi.",
      "toast.avatar": "Rasm yangilandi.",
      "toast.avatarFailed": "Rasm yuklanmadi (PNG/JPEG, 4 MB gacha).",
      "toast.keyAdded": "Kalit qoʻshildi.",
      "toast.keyExists": "Bu kalit allaqachon roʻyxatda.",
      "chip.1": "Bugun nima qilamiz?",
      "chip.2": "Eslatma qoʻy: ertaga 9:00 da uchrashuv.",
      "chip.3": "Kompyuterda nima bor?",
    },
    ru: {
      "app.title": "Ksenos Messenger",
      "gate.title": "Код доступа",
      "gate.sub": "Введите 4-значный код, чтобы войти в аккаунт",
      "gate.dotsAria": "PIN-код",
      "gate.account": "Создать аккаунт",
      "gate.noAccount": "Аккаунта пока нет. Начните с кнопки ниже.",
      "gate.locked": "Код откроется через {time}",
      "gate.already": "Аккаунт уже создан: один аккаунт, один постоянный код.",
      "gate.setupTitle": "Придумайте PIN",
      "gate.setupSub": "Этот 4-значный код постоянный: задаётся один раз и не меняется",
      "gate.repeatTitle": "Повторите код",
      "gate.repeatSub": "Введите код ещё раз, чтобы не ошибиться",
      "gate.wrong": "Неверный код. Попробуйте снова.",
      "gate.mismatch": "Коды не совпали. Начните заново.",
      "gate.tooMany": "Слишком много попыток. Повторите через минуту.",
      "gate.offline": "Сервер не отвечает. Проверьте, что main.py запущен.",
      "gate.saved": "Код сохранён. Открываем…",
      "rail.lock": "Заблокировать",
      "rail.new": "Новый чат",
      "rail.search": "Поиск по истории…",
      "rail.history": "История переписки",
      "rail.empty": "Переписки пока нет. Напишите первое сообщение — история сохранится здесь.",
      "rail.noMatch": "По этому запросу ничего не найдено.",
      "chat.menu": "Меню",
      "chat.online": "online · только агент",
      "chat.emptyTitle": "Начните диалог",
      "chat.emptyText": "Контакты не нужны: напишите — ответ появится здесь. История сохраняется сама.",
      "chat.typing": "Ksenos печатает…",
      "chat.placeholder": "Напишите сообщение…",
      "chat.send": "Отправить",
      "chat.failed": "Ответ не пришёл. Попробуйте ещё раз.",
      "day.today": "Сегодня",
      "day.yesterday": "Вчера",
      "toast.deleted": "Переписка удалена.",
      "toast.deleteFailed": "Не удалось удалить переписку.",
      "toast.locked": "Заблокировано.",
      "meta.messages": "{n} сообщ.",
      "chat.settings": "Настройки",
      "set.title": "Настройки",
      "set.close": "Закрыть",
      "set.profile": "Профиль",
      "set.upload": "Загрузить фото",
      "set.remove": "Удалить",
      "set.name": "Имя",
      "set.namePlace": "Ваше имя",
      "set.appearance": "Оформление — 6 тем",
      "set.language": "Язык",
      "set.pin": "Код доступа",
      "set.pinShow": "Показать",
      "set.pinHide": "Скрыть",
      "set.pinNote": "Код постоянный: задаётся один раз и не меняется.",
      "set.pinLock": "{n} ошибок → блокировка на {m} мин.",
      "set.ai": "ИИ и ключи",
      "set.emoji": "Использовать emoji в ответах",
      "set.apiKey": "Свой API-ключ",
      "set.apiKeyPlace": "Введите ключ",
      "set.keyAdd": "Добавить ключ",
      "set.keyNote": "Когда ключ исчерпан, приложение само переходит к следующему. Ключи хранятся на этом компьютере.",
      "set.keys": "Резервные ключи: {n}",
      "set.about": "Личный ИИ-чат для Windows. Данные остаются на этом компьютере.",
      "theme.obsidian": "Obsidian",
      "theme.midnight": "Midnight",
      "theme.emerald": "Emerald",
      "theme.sepia": "Sepia",
      "theme.violet": "Violet",
      "theme.graphite": "Graphite",
      "toast.saved": "Сохранено.",
      "toast.failed": "Не удалось сохранить.",
      "toast.avatar": "Фото обновлено.",
      "toast.avatarFailed": "Фото не загрузилось (PNG/JPEG, до 4 МБ).",
      "toast.keyAdded": "Ключ добавлен.",
      "toast.keyExists": "Этот ключ уже в списке.",
      "chip.1": "Чем займёмся сегодня?",
      "chip.2": "Напомни: завтра в 9:00 встреча.",
      "chip.3": "Что происходит на компьютере?",
    },
    en: {
      "app.title": "Ksenos Messenger",
      "gate.title": "Access code",
      "gate.sub": "Enter the 4-digit code to open your account",
      "gate.dotsAria": "PIN code",
      "gate.account": "Create an account",
      "gate.noAccount": "No account yet. Start with the button below.",
      "gate.locked": "The code reopens in {time}",
      "gate.already": "The account already exists: one account, one permanent code.",
      "gate.setupTitle": "Create your PIN",
      "gate.setupSub": "This 4-digit code is permanent: set once, never changed",
      "gate.repeatTitle": "Repeat the code",
      "gate.repeatSub": "Enter it once more so it cannot be mistyped",
      "gate.wrong": "Wrong code. Try again.",
      "gate.mismatch": "The codes do not match. Start over.",
      "gate.tooMany": "Too many attempts. Try again in a minute.",
      "gate.offline": "The server is not answering. Check that main.py is running.",
      "gate.saved": "Code saved. Opening…",
      "rail.lock": "Lock",
      "rail.new": "New chat",
      "rail.search": "Search history…",
      "rail.history": "Conversation history",
      "rail.empty": "No conversations yet. Write the first message — history is kept here.",
      "rail.noMatch": "Nothing matches that search.",
      "chat.menu": "Menu",
      "chat.online": "online · agent only",
      "chat.emptyTitle": "Start the conversation",
      "chat.emptyText": "No contacts needed: write, and the answer lands here. History saves itself.",
      "chat.typing": "Ksenos is typing…",
      "chat.placeholder": "Write a message…",
      "chat.send": "Send",
      "chat.failed": "No answer came back. Please try again.",
      "day.today": "Today",
      "day.yesterday": "Yesterday",
      "toast.deleted": "Conversation deleted.",
      "toast.deleteFailed": "The conversation could not be deleted.",
      "toast.locked": "Locked.",
      "meta.messages": "{n} messages",
      "chat.settings": "Settings",
      "set.title": "Settings",
      "set.close": "Close",
      "set.profile": "Profile",
      "set.upload": "Upload a picture",
      "set.remove": "Remove",
      "set.name": "Name",
      "set.namePlace": "Your name",
      "set.appearance": "Look — 6 themes",
      "set.language": "Language",
      "set.pin": "Access code",
      "set.pinShow": "Show",
      "set.pinHide": "Hide",
      "set.pinNote": "The code is permanent: set once, never changed.",
      "set.pinLock": "{n} wrong tries → a {m}-minute lock.",
      "set.ai": "AI and keys",
      "set.emoji": "Use emoji in replies",
      "set.apiKey": "Your own API key",
      "set.apiKeyPlace": "Paste the key",
      "set.keyAdd": "Add key",
      "set.keyNote": "When a key runs out the app moves to the next one by itself. Keys stay on this computer.",
      "set.keys": "Backup keys: {n}",
      "set.about": "A private AI chat for Windows. Your data stays on this computer.",
      "theme.obsidian": "Obsidian",
      "theme.midnight": "Midnight",
      "theme.emerald": "Emerald",
      "theme.sepia": "Sepia",
      "theme.violet": "Violet",
      "theme.graphite": "Graphite",
      "toast.saved": "Saved.",
      "toast.failed": "Could not save.",
      "toast.avatar": "Picture updated.",
      "toast.avatarFailed": "The picture did not upload (PNG/JPEG, under 4 MB).",
      "toast.keyAdded": "Key added.",
      "toast.keyExists": "That key is already in the list.",
      "chip.1": "What are we doing today?",
      "chip.2": "Remind me: meeting tomorrow at 9:00.",
      "chip.3": "What's happening on the computer?",
    },
  };

  /* Strings for the settings window and the thread actions, kept apart
     from the base table so the three languages stay side by side. */
  Object.assign(STRINGS.uz, {
    "set.appearanceShort": "Koʻrinish",
    "set.aboutShort": "Ilova",
    "set.profileLead": "Bu ism va rasm faqat shu qurilmada koʻrinadi.",
    "set.lookLead": "Har bir dizayn — alohida kayfiyat: rang, fon va xabar uslubi.",
    "set.lookNote": "Tanlov darhol qoʻllanadi va keyingi ochilishda ham saqlanadi.",
    "set.languageLead": "Interfeys tili. Brauzer tilidan avtomatik tanlanadi.",
    "set.data": "Maʼlumotlar",
    "set.dataLead": "Suhbatlar shu kompyuterda saqlanadi. Nusxasini olib, boshqa qurilmaga koʻchirish mumkin.",
    "set.export": "Tarixni yuklab olish",
    "set.exportNote": "Barcha suhbatlar bitta JSON faylda — zaxira yoki boshqa qurilma uchun.",
    "set.exportBtn": "Yuklab olish",
    "set.counts": "Saqlangan suhbatlar",
    "set.dataNote": "Boshqa qurilmada ilovani oʻrnatib, shu PIN bilan ochasiz — suhbatlar oʻsha qurilmaning bazasida boʻladi.",
    "set.version": "Versiya",
    "set.platform": "Platforma",
    "set.storage": "Baza",
    "set.pinUnavailable": "Bu kod yangilanishdan oldin yaratilgan — nusxasi saqlanmagan.",
    "rename.title": "Suhbat nomi",
    "rename.placeholder": "Nom yozing",
    "rename.save": "Saqlash",
    "rename.cancel": "Bekor qilish",
    "toast.renamed": "Nom oʻzgartirildi.",
    "toast.renameFailed": "Nomni oʻzgartirib boʻlmadi.",
    "hist.rename": "Suhbatni nomlash",
    "hist.delete": "Suhbatni oʻchirish",
    "data.summary": "{c} suhbat · {m} xabar",
  });

  Object.assign(STRINGS.ru, {
    "set.appearanceShort": "Оформление",
    "set.aboutShort": "Приложение",
    "set.profileLead": "Это имя и фото видны только на этом устройстве.",
    "set.lookLead": "Каждая тема — своё настроение: цвет, фон и стиль сообщений.",
    "set.lookNote": "Выбор применяется сразу и сохраняется до следующего запуска.",
    "set.languageLead": "Язык интерфейса. Выбирается автоматически по языку системы.",
    "set.data": "Данные",
    "set.dataLead": "Переписки хранятся на этом компьютере. Можно сохранить копию и перенести на другое устройство.",
    "set.export": "Скачать историю",
    "set.exportNote": "Все переписки в одном JSON-файле — для резервной копии или другого устройства.",
    "set.exportBtn": "Скачать",
    "set.counts": "Сохранённые переписки",
    "set.dataNote": "На другом устройстве установите приложение и откройте его тем же PIN — переписки будут в базе того устройства.",
    "set.version": "Версия",
    "set.platform": "Платформа",
    "set.storage": "База",
    "set.pinUnavailable": "Этот код создан до обновления — копия не сохранилась.",
    "rename.title": "Название переписки",
    "rename.placeholder": "Введите название",
    "rename.save": "Сохранить",
    "rename.cancel": "Отмена",
    "toast.renamed": "Название изменено.",
    "toast.renameFailed": "Не удалось изменить название.",
    "hist.rename": "Переименовать переписку",
    "hist.delete": "Удалить переписку",
    "data.summary": "{c} переписок · {m} сообщений",
  });

  Object.assign(STRINGS.en, {
    "set.appearanceShort": "Appearance",
    "set.aboutShort": "App",
    "set.profileLead": "This name and picture are shown on this device only.",
    "set.lookLead": "Each design has its own mood: colour, background and bubble style.",
    "set.lookNote": "The choice applies at once and is remembered next time.",
    "set.languageLead": "Interface language. Picked from your system language.",
    "set.data": "Data",
    "set.dataLead": "Conversations live on this computer. Take a copy to move them to another device.",
    "set.export": "Download history",
    "set.exportNote": "Every conversation in one JSON file — for a backup or another device.",
    "set.exportBtn": "Download",
    "set.counts": "Saved conversations",
    "set.dataNote": "On another device install the app and open it with the same PIN — conversations then live in that device's database.",
    "set.version": "Version",
    "set.platform": "Platform",
    "set.storage": "Database",
    "set.pinUnavailable": "This code predates the update — no copy was stored.",
    "rename.title": "Conversation name",
    "rename.placeholder": "Type a name",
    "rename.save": "Save",
    "rename.cancel": "Cancel",
    "toast.renamed": "Name changed.",
    "toast.renameFailed": "The name could not be changed.",
    "hist.rename": "Rename conversation",
    "hist.delete": "Delete conversation",
    "data.summary": "{c} conversations · {m} messages",
  });

  Object.assign(STRINGS.uz, {
    "rail.searchAria": "Tarixdan qidirish",
    "rail.collapse": "Yon panelni yopish",
    "call.start": "Qoʻngʻiroq",
    "call.connecting": "Ulanmoqda…",
    "call.live": "Qoʻngʻiroq · {time}",
    "call.listening": "Tinglayapman…",
    "call.thinking": "Javob tayyorlanmoqda…",
    "call.speaking": "Ksenos gapirayapti…",
    "call.hint": "Mikrofonni bosing va gapiring — javob ovoz bilan keladi.",
    "call.noMic": "Bu brauzer mikrofonni tushunmaydi. Xabarni yozib yuboring.",
    "call.denied": "Mikrofon ruxsati berilmagan. Manzil yonidagi mikrofon belgisidan ruxsat bering, keyin qayta bosing.",
    "call.allow": "Mikrofon ruxsatini tasdiqlang…",
    "call.mic": "Mikrofon",
    "call.end": "Tugatish",
    "call.ended": "Qoʻngʻiroq tugadi.",
    "call.empty": "Hech narsa eshitilmadi.",
  });

  Object.assign(STRINGS.ru, {
    "rail.searchAria": "Поиск по истории",
    "rail.collapse": "Скрыть боковую панель",
    "call.start": "Звонок",
    "call.connecting": "Соединяем…",
    "call.live": "Звонок · {time}",
    "call.listening": "Слушаю…",
    "call.thinking": "Готовлю ответ…",
    "call.speaking": "Ksenos говорит…",
    "call.hint": "Нажмите микрофон и говорите — ответ придёт голосом.",
    "call.noMic": "Этот браузер не понимает микрофон. Напишите сообщение.",
    "call.denied": "Доступ к микрофону не дан. Разрешите его в значке микрофона рядом с адресом и нажмите снова.",
    "call.allow": "Подтвердите доступ к микрофону…",
    "call.mic": "Микрофон",
    "call.end": "Завершить",
    "call.ended": "Звонок завершён.",
    "call.empty": "Ничего не услышано.",
  });

  Object.assign(STRINGS.en, {
    "rail.searchAria": "Search history",
    "rail.collapse": "Hide sidebar",
    "call.start": "Call",
    "call.connecting": "Connecting…",
    "call.live": "Call · {time}",
    "call.listening": "Listening…",
    "call.thinking": "Preparing a reply…",
    "call.speaking": "Ksenos is speaking…",
    "call.hint": "Press the microphone and speak — the reply comes back as voice.",
    "call.noMic": "This browser cannot listen to a microphone. Type the message instead.",
    "call.denied": "Microphone access was refused. Allow it from the microphone icon next to the address, then press again.",
    "call.allow": "Allow the microphone prompt…",
    "call.mic": "Microphone",
    "call.end": "End",
    "call.ended": "Call ended.",
    "call.empty": "Nothing was heard.",
  });

  /* Another device, reminders in the window and the call fallbacks. */
  Object.assign(STRINGS.uz, {
    "gate.pairLink": "Boshqa qurilmadan ulanish",
    "gate.pairCode": "Ulash kodi",
    "gate.pairName": "Qurilma nomi",
    "gate.pairNamePlace": "Masalan: Noutbuk",
    "gate.pairGo": "Ulash",
    "gate.pairCancel": "Bekor qilish",
    "gate.pairHint": "Kodni shu kompyuterda: Sozlamalar → Ulangan qurilmalar boʻlimidan oling.",
    "gate.pairWrong": "Kod notoʻgʻri yoki eskirgan. Yangi kod oling.",
    "gate.pairOk": "Ulandi. Ochilmoqda…",
    "gate.pairOffline": "Server javob bermayapti. Manzilni tekshiring.",
    "set.devices": "Ulangan qurilmalar",
    "set.devicesLead": "Boshqa kompyuter yoki telefonda shu manzilni ochib, ulash kodini kiriting — suhbat, tarix va sozlamalar bir xil boʻladi.",
    "set.devAddress": "Manzil",
    "set.devCopy": "Nusxalash",
    "set.devCopied": "Manzil nusxalandi.",
    "set.devAdd": "Yangi qurilma qoʻshish",
    "set.devAddNote": "Kod 10 daqiqa amal qiladi va bir marta ishlaydi.",
    "set.devCode": "Kod olish",
    "set.devYourCode": "Ulash kodi",
    "set.devLeft": "{m} daqiqa amal qiladi",
    "set.devEmpty": "Hali boshqa qurilma ulanmagan.",
    "set.devRemove": "Uzish",
    "set.devThis": "shu qurilma",
    "set.devSeen": "Oxirgi marta: {when}",
    "set.devLanOn": "Shu manzilni boshqa qurilma brauzerida oching. Windows Firewall soʻrasa — ruxsat bering.",
    "set.devLanOff": "Server hozir faqat shu kompyuterni koʻradi. run.bat ni ishga tushiring — u tarmoqqa ochadi.",
    "call.typePlace": "Yozib yuborish…",
    "call.send": "Yuborish",
    "call.heard": "Siz: {text}",
    "call.reply": "Ksenos: {text}",
    "call.listeningNow": "Tinglayapman… gapiring",
    "call.noDevice": "Mikrofon topilmadi.",
    "call.network": "Nutq xizmatiga internet kerak.",
    "call.micFailed": "Mikrofon xatosi: {code}",
    "call.langFallback": "Bu tizim {lang} tilini tushunmaydi — davom etaman.",
    "call.reminder": "Eslatma: {text}",
    "call.ringing": "Qoʻngʻiroq vaqti keldi",
  });

  Object.assign(STRINGS.ru, {
    "gate.pairLink": "Подключиться с другого устройства",
    "gate.pairCode": "Код подключения",
    "gate.pairName": "Название устройства",
    "gate.pairNamePlace": "Например: Ноутбук",
    "gate.pairGo": "Подключить",
    "gate.pairCancel": "Отмена",
    "gate.pairHint": "Код создаётся на этом компьютере: Настройки → Подключённые устройства.",
    "gate.pairWrong": "Код неверный или истёк. Создайте новый.",
    "gate.pairOk": "Подключено. Открываем…",
    "gate.pairOffline": "Сервер не отвечает. Проверьте адрес.",
    "set.devices": "Подключённые устройства",
    "set.devicesLead": "Откройте этот адрес на другом компьютере или телефоне и введите код подключения — чат, история и настройки будут общими.",
    "set.devAddress": "Адрес",
    "set.devCopy": "Копировать",
    "set.devCopied": "Адрес скопирован.",
    "set.devAdd": "Добавить устройство",
    "set.devAddNote": "Код действует 10 минут и работает один раз.",
    "set.devCode": "Получить код",
    "set.devYourCode": "Код подключения",
    "set.devLeft": "Действует ещё {m} мин.",
    "set.devEmpty": "Пока нет подключённых устройств.",
    "set.devRemove": "Отключить",
    "set.devThis": "это устройство",
    "set.devSeen": "Последний раз: {when}",
    "set.devLanOn": "Откройте этот адрес в браузере другого устройства. Если Windows Firewall спросит — разрешите.",
    "set.devLanOff": "Сервер виден только на этом компьютере. Запустите run.bat — он открывает доступ по сети.",
    "call.typePlace": "Написать…",
    "call.send": "Отправить",
    "call.heard": "Вы: {text}",
    "call.reply": "Ksenos: {text}",
    "call.listeningNow": "Слушаю… говорите",
    "call.noDevice": "Микрофон не найден.",
    "call.network": "Распознаванию нужен интернет.",
    "call.micFailed": "Ошибка микрофона: {code}",
    "call.langFallback": "Эта система не понимает {lang} — продолжаю.",
    "call.reminder": "Напоминание: {text}",
    "call.ringing": "Время звонка",
  });

  Object.assign(STRINGS.en, {
    "gate.pairLink": "Connect from another device",
    "gate.pairCode": "Pairing code",
    "gate.pairName": "Device name",
    "gate.pairNamePlace": "For example: Laptop",
    "gate.pairGo": "Connect",
    "gate.pairCancel": "Cancel",
    "gate.pairHint": "Create the code on this computer: Settings → Connected devices.",
    "gate.pairWrong": "The code is wrong or expired. Create a new one.",
    "gate.pairOk": "Connected. Opening…",
    "gate.pairOffline": "The server does not answer. Check the address.",
    "set.devices": "Connected devices",
    "set.devicesLead": "Open this address on another computer or phone and enter a pairing code — chat, history and settings stay the same.",
    "set.devAddress": "Address",
    "set.devCopy": "Copy",
    "set.devCopied": "Address copied.",
    "set.devAdd": "Add a device",
    "set.devAddNote": "The code works for 10 minutes and only once.",
    "set.devCode": "Get a code",
    "set.devYourCode": "Pairing code",
    "set.devLeft": "Valid for {m} more min",
    "set.devEmpty": "No other device is connected yet.",
    "set.devRemove": "Disconnect",
    "set.devThis": "this device",
    "set.devSeen": "Last seen: {when}",
    "set.devLanOn": "Open this address in another device's browser. If Windows Firewall asks, allow it.",
    "set.devLanOff": "The server only listens on this computer. Start run.bat — it opens the network address.",
    "call.typePlace": "Type a message…",
    "call.send": "Send",
    "call.heard": "You: {text}",
    "call.reply": "Ksenos: {text}",
    "call.listeningNow": "Listening… speak now",
    "call.noDevice": "No microphone was found.",
    "call.network": "Speech recognition needs the internet.",
    "call.micFailed": "Microphone error: {code}",
    "call.langFallback": "This system has no {lang} recogniser — continuing.",
    "call.reminder": "Reminder: {text}",
    "call.ringing": "Time for the call",
  });

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const STORE_KEY = "ksenus-messenger-lang";

  const state = {
    lang: "uz",
    unlocked: false,
    pinSet: false,
    pin: "",
    stage: "unlock", // unlock | setup | confirm
    firstPin: "",
    conversations: [],
    activeId: null,
    sending: false,
    settings: null,
    pinVisible: false,
    lockTimer: null,
    devices: [],
    devUrls: [],
    devLanHost: "127.0.0.1",
    inboxTimer: null,
    inboxReady: false,
    pairExpires: 0,
    pairTimer: null,
  };

  /* ---------- language ---------- */
  const detectLang = () => {
    let saved = null;
    try {
      saved = window.localStorage.getItem(STORE_KEY);
    } catch (error) {
      saved = null;
    }
    if (saved && STRINGS[saved]) return saved;

    const candidates = (navigator.languages && navigator.languages.length
      ? Array.from(navigator.languages)
      : [navigator.language || ""]
    ).map((code) => String(code).toLowerCase());

    for (const code of candidates) {
      if (code.indexOf("uz") === 0) return "uz";
      if (code.indexOf("ru") === 0) return "ru";
      if (code.indexOf("en") === 0) return "en";
    }
    return "uz";
  };

  const t = (key, vars) => {
    const table = STRINGS[state.lang] || STRINGS.uz;
    let text = table[key] !== undefined ? table[key] : STRINGS.uz[key];
    if (text === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach((name) => {
        text = text.split(`{${name}}`).join(String(vars[name]));
      });
    }
    return text;
  };

  const applyLanguage = () => {
    document.documentElement.lang = state.lang === "uz" ? "uz-Latn" : state.lang;
    document.title = t("app.title");
    $$("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n);
    });
    $$("[data-i18n-attr]").forEach((element) => {
      element.dataset.i18nAttr.split(",").forEach((pair) => {
        const parts = pair.split(":");
        if (parts.length !== 2) return;
        element.setAttribute(parts[0].trim(), t(parts[1].trim()));
      });
    });
    $$("[data-i18n-aria]").forEach((element) => {
      element.setAttribute("aria-label", t(element.dataset.i18nAria));
    });
    $$(".lang button, .gate-lang button, #setLang button").forEach((button) => {
      const active = button.dataset.lang === state.lang;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $$(".chip").forEach((chip, index) => {
      const key = `chip.${index + 1}`;
      chip.textContent = t(key);
      chip.dataset.prompt = t(key);
    });
    paintHistory();
  };

  const setLanguage = (lang, { persist = true } = {}) => {
    if (!STRINGS[lang]) return;
    state.lang = lang;
    if (persist) {
      try {
        window.localStorage.setItem(STORE_KEY, lang);
      } catch (error) {
        /* the choice simply does not persist */
      }
    }
    applyLanguage();
  };

  /* ---------- small helpers ---------- */
  const toast = (message) => {
    const box = $("#toast");
    if (!box) return;
    box.textContent = message;
    box.classList.add("show");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => box.classList.remove("show"), 3200);
  };

  const clock = (value) => {
    const date = value ? new Date(value) : new Date();
    return date.toLocaleTimeString(state.lang === "en" ? "en-GB" : state.lang, {
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const dayLabel = (value) => {
    const date = value ? new Date(value) : new Date();
    const today = new Date();
    const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const diff = Math.round((midnight(today) - midnight(date)) / 86_400_000);
    if (diff === 0) return t("day.today");
    if (diff === 1) return t("day.yesterday");
    return date.toLocaleDateString(state.lang === "en" ? "en-GB" : state.lang, {
      day: "numeric",
      month: "short",
    });
  };

  const shortDate = (value) => {
    if (!value) return "";
    return new Date(value).toLocaleDateString(state.lang === "en" ? "en-GB" : state.lang, {
      day: "numeric",
      month: "short",
    });
  };

  const escapeHtml = (text) =>
    String(text).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));

  const api = async (path, options = {}) => {
    const response = await fetch(`${API}${path}`, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (response.status === 401) {
      showGate("unlock");
      throw new Error("locked");
    }
    return response;
  };

  /* ---------- sprite ---------- */
  const animateSprite = () => {
    const image = $("#avatarSprite");
    if (!image || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    for (let index = 0; index < 4; index += 1) {
      const preload = new Image();
      preload.src = `${SPRITE}/idle-${index}.png`;
    }
    let frame = 0;
    window.setInterval(() => {
      frame = (frame + 1) % 4;
      image.src = `${SPRITE}/idle-${frame}.png`;
    }, 320);
  };

  /* ---------- PIN gate ---------- */
  const paintDots = () => {
    $$("#dots .dot").forEach((dot, index) => {
      dot.classList.toggle("filled", index < state.pin.length);
    });
  };

  const gateMessage = (text, kind) => {
    const box = $("#gateMsg");
    if (!box) return;
    box.textContent = text || "";
    box.className = `gate-msg${kind ? ` ${kind}` : ""}`;
  };

  const gateTexts = () => {
    const title = $("#gateTitle");
    const sub = $("#gateSub");
    const account = $("#gateAccount");
    if (account) account.hidden = state.pinSet && state.stage === "unlock";
    const pairLink = $("#gatePairOpen");
    if (pairLink) pairLink.hidden = state.stage !== "unlock";
    if (state.stage === "unlock" && !state.pinSet) {
      title.textContent = t("gate.title");
      sub.textContent = t("gate.noAccount");
      return;
    }
    if (state.stage === "setup") {
      title.textContent = t("gate.setupTitle");
      sub.textContent = t("gate.setupSub");
    } else if (state.stage === "confirm") {
      title.textContent = t("gate.repeatTitle");
      sub.textContent = t("gate.repeatSub");
    } else {
      title.textContent = t("gate.title");
      sub.textContent = t("gate.sub");
    }
  };

  const showGate = (stage) => {
    state.stage = stage;
    state.pin = "";
    state.firstPin = "";
    state.unlocked = false;
    paintDots();
    gateTexts();
    gateMessage("");
    const pairForm = $("#gatePair");
    if (pairForm) pairForm.hidden = true;
    $("#gate").hidden = false;
    $("#app").hidden = true;
  };


  const openApp = () => {
    state.unlocked = true;
    $("#gate").hidden = true;
    $("#app").hidden = false;
    animateSprite();
    loadHistory();
    loadSettings();
    startInbox();
  };

  /* ---------- another device ---------- */
  const openPairForm = (show) => {
    const form = $("#gatePair");
    if (form) form.hidden = !show;
    const field = $("#pairCode");
    if (show) {
      window.setTimeout(() => field?.focus(), 40);
    } else if (field) {
      field.value = "";
    }
  };

  const pairThisDevice = async () => {
    const code = ($("#pairCode")?.value || "").replace(/\D/g, "");
    const name = ($("#pairName")?.value || "").trim() || "Qurilma";
    if (code.length !== 6) {
      gateMessage(t("gate.pairWrong"), "error");
      return;
    }
    try {
      const response = await fetch(`${API}/devices/pair`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, name }),
      });
      if (!response.ok) {
        gateMessage(
          response.status === 401 ? t("gate.pairWrong") : t("gate.pairOffline"),
          "error"
        );
        return;
      }
      gateMessage(t("gate.pairOk"), "ok");
      openPairForm(false);
      const status = await fetch(`${API}/status`, { credentials: "same-origin" });
      const data = await status.json().catch(() => ({}));
      state.pinSet = Boolean(data.pin_set);
      openApp();
    } catch (error) {
      gateMessage(t("gate.pairOffline"), "error");
    }
  };

  const submitPin = async () => {
    const pin = state.pin;

    if (state.stage === "setup") {
      state.firstPin = pin;
      state.pin = "";
      state.stage = "confirm";
      paintDots();
      gateTexts();
      gateMessage("");
      return;
    }

    if (state.stage === "confirm") {
      if (pin !== state.firstPin) {
        showGate("setup");
        gateMessage(t("gate.mismatch"), "error");
        $("#gateCard").classList.add("shake");
        window.setTimeout(() => $("#gateCard").classList.remove("shake"), 450);
        return;
      }
      try {
        const created = await fetch(`${API}/pin`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pin }),
        });
        if (!created.ok && created.status !== 409) throw new Error("pin");
        await unlock(pin, t("gate.saved"));
      } catch (error) {
        gateMessage(t("gate.offline"), "error");
      }
      return;
    }

    await unlock(pin);
  };

  const unlock = async (pin, successMessage) => {
    try {
      const response = await fetch(`${API}/unlock`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      if (response.ok) {
        if (successMessage) gateMessage(successMessage, "ok");
        state.pin = "";
        state.firstPin = "";
        paintDots();
        openApp();
        return;
      }
      const data = await response.json().catch(() => ({}));
      gateMessage(
        response.status === 429 ? t("gate.tooMany") : t("gate.wrong"),
        "error"
      );
      state.pin = "";
      paintDots();
      $("#gateCard").classList.add("shake");
      window.setTimeout(() => $("#gateCard").classList.remove("shake"), 450);
      if (data.detail) {
        /* the server's sentence is more precise than ours; keep it in the log */
        console.debug("unlock:", data.detail);
      }
    } catch (error) {
      gateMessage(t("gate.offline"), "error");
    }
  };

  const pressKey = (key) => {
    if (key === "clear") {
      state.pin = "";
      paintDots();
      return;
    }
    if (key === "back") {
      state.pin = state.pin.slice(0, -1);
      paintDots();
      return;
    }
    if (state.pin.length >= 4) return;
    state.pin += key;
    paintDots();
    if (state.pin.length === 4) {
      window.setTimeout(submitPin, 140);
    }
  };

  /* ---------- history ---------- */
  const renderHistory = () => {
    const list = $("#historyList");
    const count = $("#historyCount");
    if (!list) return;
    if (count) count.textContent = String(state.conversations.length);

    if (!state.conversations.length) {
      list.innerHTML = `<div class="history-empty">${escapeHtml(
        $("#searchInput")?.value ? t("rail.noMatch") : t("rail.empty")
      )}</div>`;
      return;
    }

    list.innerHTML = state.conversations
      .map((item) => {
        const meta = `${t("meta.messages", { n: item.message_count })} · ${shortDate(item.updated_at)}`;
        return `
          <div class="hitem${item.id === state.activeId ? " active" : ""}" data-id="${item.id}" role="listitem" tabindex="0">
            <span class="h-title">${escapeHtml(item.title)}</span>
            <span class="h-meta">${escapeHtml(meta)}</span>
            <span class="h-acts">
              <button class="h-act" type="button" data-rename="${item.id}" aria-label="${escapeHtml(t("hist.rename"))}" title="${escapeHtml(t("hist.rename"))}">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 20h4l9.5-9.5a2.1 2.1 0 0 0-3-3L5 17v3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
              </button>
              <button class="h-act danger" type="button" data-del="${item.id}" aria-label="${escapeHtml(t("hist.delete"))}" title="${escapeHtml(t("hist.delete"))}">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 7h14M10 7V5.5A1.5 1.5 0 0 1 11.5 4h1A1.5 1.5 0 0 1 14 5.5V7m-7 0 .7 12a1.5 1.5 0 0 0 1.5 1.4h5.6a1.5 1.5 0 0 0 1.5-1.4L17 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
              </button>
            </span>
          </div>`;
      })
      .join("");
  };

  const paintHistory = () => {
    if (state.unlocked) renderHistory();
  };

  const loadHistory = async (query = "") => {
    try {
      const response = await api(`/history?query=${encodeURIComponent(query)}`);
      if (!response.ok) return;
      const data = await response.json();
      state.conversations = data.conversations || [];
      renderHistory();
    } catch (error) {
      /* a failed list refresh never breaks the thread */
    }
  };

  const openConversation = async (id) => {
    state.activeId = id;
    renderHistory();
    closeRail();
    try {
      const response = await api(`/history/${id}`);
      if (!response.ok) throw new Error("load");
      const data = await response.json();
      renderThread(data.messages || []);
      const badge = $("#threadBadge");
      if (badge) {
        badge.textContent = data.title || "";
        badge.hidden = !data.title;
      }
      const edit = $("#threadEdit");
      if (edit) edit.hidden = false;
    } catch (error) {
      toast(t("chat.failed"));
    }
  };

  const deleteConversation = async (id) => {
    try {
      const response = await api(`/history/${id}`, { method: "DELETE" });
      if (!response.ok) throw new Error("delete");
      state.conversations = state.conversations.filter((item) => item.id !== id);
      if (state.activeId === id) {
        state.activeId = null;
        showEmpty();
      }
      renderHistory();
      toast(t("toast.deleted"));
    } catch (error) {
      toast(t("toast.deleteFailed"));
    }
  };

  /* ---------- thread ---------- */
  const thread = $("#thread");
  let renderedDay = null;

  const showEmpty = () => {
    renderedDay = null;
    const badge = $("#threadBadge");
    if (badge) badge.hidden = true;
    const edit = $("#threadEdit");
    if (edit) edit.hidden = true;
    const counts = $("#dataCounts");
    if (counts) counts.textContent = "—";
    thread.innerHTML = `
      <div class="empty" id="emptyState">
        <img class="empty-robot" src="${SPRITE}/idle-1.png" alt="" />
        <h2>${escapeHtml(t("chat.emptyTitle"))}</h2>
        <p>${escapeHtml(t("chat.emptyText"))}</p>
        <div class="chips">
          ${[1, 2, 3]
            .map(
              (index) =>
                `<button type="button" class="chip" data-prompt="${escapeHtml(t(`chip.${index}`))}">${escapeHtml(t(`chip.${index}`))}</button>`
            )
            .join("")}
        </div>
      </div>`;
  };

  const atBottom = () =>
    thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;

  const scrollDown = (smooth = true) => {
    thread.scrollTo({
      top: thread.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
  };

  const appendMessage = (role, text, createdAt, extraClass = "") => {
    const keep = atBottom();
    const stamp = createdAt || new Date().toISOString();
    const label = dayLabel(stamp);

    if (renderedDay !== label) {
      const day = document.createElement("div");
      day.className = "day";
      day.dataset.day = label;
      day.textContent = label;
      thread.appendChild(day);
      renderedDay = label;
    }

    const bubble = document.createElement("div");
    bubble.className = `msg ${role === "user" ? "out" : "in"}${extraClass ? ` ${extraClass}` : ""}`;
    bubble.innerHTML = `${escapeHtml(text)}<time>${escapeHtml(clock(stamp))}</time>`;
    thread.appendChild(bubble);
    if (keep) scrollDown();
    return bubble;
  };

  const renderThread = (messages) => {
    thread.innerHTML = "";
    renderedDay = null;
    if (!messages.length) {
      showEmpty();
      return;
    }
    const badge = $("#threadBadge");
    if (badge) badge.hidden = true;
    messages.forEach((message) => {
      appendMessage(message.role === "user" ? "user" : "agent", message.content, message.created_at);
    });
    requestAnimationFrame(() => scrollDown(false));
  };

  /* ---------- sending ---------- */
  const input = $("#input");
  const sendButton = $("#sendBtn");
  const typing = $("#typing");

  const autoGrow = () => {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
  };

  const send = async (text) => {
    const message = (text || "").trim();
    if (!message || state.sending) return;

    state.sending = true;
    let replyText = null;
    if (sendButton) sendButton.disabled = true;
    if ($("#emptyState")) {
      thread.innerHTML = "";
      renderedDay = null;
    }

    appendMessage("user", message, null, "pending");
    scrollDown();
    if (input) {
      input.value = "";
      autoGrow();
    }
    if (typing) typing.hidden = false;
    scrollDown();

    try {
      const response = await api("/send", {
        method: "POST",
        body: JSON.stringify({
          message,
          conversation_id: state.activeId || null,
          emoji: state.settings ? Boolean(state.settings.emoji) : true,
        }),
      });
      if (!response.ok) throw new Error("send");
      const data = await response.json();
      state.activeId = data.conversation_id || state.activeId;
      $$(".msg.pending").forEach((bubble) => bubble.classList.remove("pending"));
      replyText = data.reply || "";
      appendMessage("agent", replyText || "…");
      await loadHistory($("#searchInput")?.value || "");
    } catch (error) {
      $$(".msg.pending").forEach((bubble) => bubble.classList.remove("pending"));
      if (error.message !== "locked") {
        appendMessage("agent", t("chat.failed"), null, "error");
      }
    } finally {
      state.sending = false;
      if (sendButton) sendButton.disabled = false;
      if (typing) typing.hidden = true;
      scrollDown();
      input?.focus();
    }

    return replyText;
  };

  /* ---------- call ---------- */
  const call = {
    active: false,
    seconds: 0,
    timer: null,
    recognition: null,
    listening: false,
    wantListen: false,
    lang: "",
  };

  const callLine = (role, text) => {
    const log = $("#callLog");
    if (!log || !text) return;
    const line = document.createElement("div");
    line.className = `call-line${role === "out" ? " out" : role === "system" ? " system" : ""}`;
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  };

  /* The reply is spoken in a voice the computer actually has. A missing
     uz-UZ voice used to make speak() silent, so the call looked one-way. */
  const voiceFor = (langs) => {
    if (!("speechSynthesis" in window)) return null;
    const voices = window.speechSynthesis.getVoices() || [];
    for (const lang of langs) {
      const exact = voices.find((voice) => voice.lang && voice.lang.toLowerCase() === lang.toLowerCase());
      if (exact) return exact;
    }
    for (const lang of langs) {
      const base = lang.slice(0, 2).toLowerCase();
      const near = voices.find((voice) => voice.lang && voice.lang.toLowerCase().startsWith(base));
      if (near) return near;
    }
    return null;
  };

  const speechLang = () => (state.lang === "uz" ? "uz-UZ" : state.lang === "ru" ? "ru-RU" : "en-US");

  if ("speechSynthesis" in window) {
    // Chromium fills the voice list asynchronously; asking early fills the cache.
    window.speechSynthesis.getVoices();
    window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
  }

  const callClock = (total) => {
    const minutes = String(Math.floor(total / 60)).padStart(2, "0");
    const rest = String(total % 60).padStart(2, "0");
    return `${minutes}:${rest}`;
  };

  const callStatus = (text) => {
    const node = $("#callStatus");
    if (node) node.textContent = text;
  };

  const callHint = (text) => {
    const node = $("#callHint");
    if (node) node.textContent = text;
  };

  const speak = (text) => {
    if (!text || !("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      const saying = new SpeechSynthesisUtterance(text);
      const lang = speechLang();
      const voice = voiceFor(
        state.lang === "uz" ? [lang, "ru-RU", "en-US"] : state.lang === "ru" ? [lang, "en-US"] : [lang]
      );
      saying.lang = voice ? voice.lang : lang;
      if (voice) saying.voice = voice;
      saying.rate = 1;
      saying.onstart = () => call.active && callStatus(t("call.speaking"));
      saying.onend = () => {
        if (!call.active) return;
        callStatus(t("call.live", { time: callClock(call.seconds) }));
        if (call.wantListen) startRecognition();
      };
      window.speechSynthesis.speak(saying);
    } catch (error) {
      /* a silent reply still shows in the thread and the call log */
    }
  };

  const stopSpeaking = () => {
    try {
      window.speechSynthesis.cancel();
    } catch (error) {
      /* nothing was speaking */
    }
  };

  const speechRecognition = () =>
    window.SpeechRecognition || window.webkitSpeechRecognition || null;

  const stopListening = (keepWish = false) => {
    if (!keepWish) call.wantListen = false;
    if (call.recognition && call.listening) {
      try {
        call.recognition.stop();
      } catch (error) {
        /* already stopped */
      }
    }
    call.listening = false;
    $("#callMic")?.classList.remove("live");
  };

  const micError = (code) => {
    if (code === "not-allowed" || code === "service-not-allowed") return t("call.denied");
    if (code === "audio-capture") return t("call.noDevice");
    if (code === "network") return t("call.network");
    if (code === "no-speech") return t("call.empty");
    return t("call.micFailed", { code: code || "?" });
  };

  const handleHeard = async (heard) => {
    callLine("out", t("call.heard", { text: heard }));
    callHint("");
    callStatus(t("call.thinking"));
    const reply = await send(heard);
    if (!call.active) return;
    if (reply) {
      callLine("in", t("call.reply", { text: reply }));
      speak(reply);
    } else {
      callStatus(t("call.live", { time: callClock(call.seconds) }));
    }
  };

  function startRecognition() {
    const Recognition = speechRecognition();
    if (!Recognition || !call.active) return;
    if (!call.recognition) {
      call.recognition = new Recognition();
      call.recognition.continuous = true;
      call.recognition.interimResults = true;
      call.recognition.maxAlternatives = 1;

      call.recognition.onresult = (event) => {
        let interim = "";
        let final = "";
        for (let index = event.resultIndex || 0; index < event.results.length; index += 1) {
          const chunk = event.results[index][0].transcript || "";
          if (event.results[index].isFinal) final += chunk;
          else interim += chunk;
        }
        if (interim.trim()) callHint(interim.trim());
        if (!final.trim()) return;
        stopListening();
        handleHeard(final.trim());
      };

      call.recognition.onerror = (event) => {
        const code = event.error || "";
        const restart = call.wantListen && call.active;
        call.listening = false;
        $("#callMic")?.classList.remove("live");
        if (!call.active) return;
        if (code === "no-speech" || code === "aborted") {
          if (restart) window.setTimeout(startRecognition, 400);
          return;
        }
        if (code === "language-not-supported") {
          // Some builds have no uz-UZ recogniser; keep the call usable.
          call.lang = call.lang ? "" : state.lang === "uz" ? "ru-RU" : "en-US";
          callHint(t("call.langFallback", { lang: call.lang || speechLang() }));
          if (restart) window.setTimeout(startRecognition, 300);
          return;
        }
        call.wantListen = false;
        callHint(micError(code));
      };

      call.recognition.onend = () => {
        call.listening = false;
        $("#callMic")?.classList.remove("live");
        if (call.wantListen && call.active) window.setTimeout(startRecognition, 350);
      };
    }

    stopSpeaking();
    call.recognition.lang = call.lang || speechLang();
    try {
      call.recognition.start();
      call.listening = true;
      $("#callMic")?.classList.add("live");
      callStatus(t("call.listening"));
      callHint(t("call.listeningNow"));
    } catch (error) {
      /* start() twice in a row throws; the running session is enough */
    }
  }

  const listen = () => {
    const Recognition = speechRecognition();
    if (!Recognition) {
      callHint(t("call.noMic"));
      return;
    }
    call.wantListen = true;
    // Trigger the browser's microphone prompt, but never wait for it: in an
    // app window the promise can stay pending, and then the button used to
    // look dead. Recognition reports the real error if permission is refused.
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        navigator.mediaDevices
          .getUserMedia({ audio: true })
          .then((stream) => stream.getTracks().forEach((track) => track.stop()))
          .catch(() => {});
      }
    } catch (error) {
      /* recognition will say what went wrong */
    }
    callHint(t("call.allow"));
    startRecognition();
  };

  const startCall = () => {
    const sheet = $("#call");
    if (!sheet || call.active) return;
    call.active = true;
    call.seconds = 0;
    call.wantListen = false;
    call.lang = "";
    sheet.hidden = false;
    const log = $("#callLog");
    if (log) log.innerHTML = "";
    callStatus(t("call.connecting"));
    callHint(speechRecognition() ? t("call.hint") : t("call.noMic"));
    const mic = $("#callMic");
    if (mic) mic.disabled = false;

    window.setTimeout(() => {
      if (!call.active) return;
      callStatus(t("call.live", { time: "00:00" }));
      call.timer = window.setInterval(() => {
        call.seconds += 1;
        if (!call.listening) callStatus(t("call.live", { time: callClock(call.seconds) }));
      }, 1000);
    }, 700);
  };

  const endCall = () => {
    if (!call.active) return;
    call.active = false;
    call.wantListen = false;
    window.clearInterval(call.timer);
    call.timer = null;
    stopListening();
    stopSpeaking();
    const sheet = $("#call");
    if (sheet) sheet.hidden = true;
    toast(t("call.ended"));
  };

  /* ---------- rail (narrow windows) ---------- */
  const closeRail = () => {
    $("#app")?.classList.remove("rail-open");
    const scrim = $("#scrim");
    if (scrim) scrim.hidden = true;
  };

  const toggleRail = () => {
    const app = $("#app");
    if (!app) return;
    const open = app.classList.toggle("rail-open");
    const scrim = $("#scrim");
    if (scrim) scrim.hidden = !open;
  };

  /* ---------- wiring ---------- */
  const bind = () => {
    $$(".lang button, .gate-lang button").forEach((button) => {
      button.addEventListener("click", () => setLanguage(button.dataset.lang));
    });

    $("#keypad")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-key]");
      if (button) pressKey(button.dataset.key);
    });

    document.addEventListener("keydown", (event) => {
      if (state.unlocked) return;
      if (/^\d$/.test(event.key)) pressKey(event.key);
      else if (event.key === "Backspace") pressKey("back");
      else if (event.key === "Escape") pressKey("clear");
    });

    $("#newChat")?.addEventListener("click", () => {
      state.activeId = null;
      renderHistory();
      showEmpty();
      closeRail();
      input?.focus();
    });

    let searchTimer = null;
    $("#searchInput")?.addEventListener("input", (event) => {
      window.clearTimeout(searchTimer);
      const value = event.target.value;
      searchTimer = window.setTimeout(() => loadHistory(value), 220);
    });

    $("#historyList")?.addEventListener("click", (event) => {
      const rename = event.target.closest("[data-rename]");
      if (rename) {
        event.stopPropagation();
        openRename(Number(rename.dataset.rename));
        return;
      }
      const remove = event.target.closest("[data-del]");
      if (remove) {
        event.stopPropagation();
        deleteConversation(Number(remove.dataset.del));
        return;
      }
      const item = event.target.closest(".hitem");
      if (item) openConversation(Number(item.dataset.id));
    });

    $("#historyList")?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const item = event.target.closest(".hitem");
      if (item) openConversation(Number(item.dataset.id));
    });

    thread?.addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (chip) send(chip.dataset.prompt || chip.textContent);
    });

    $("#composer")?.addEventListener("submit", (event) => {
      event.preventDefault();
      send(input?.value || "");
    });

    input?.addEventListener("input", autoGrow);
    input?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send(input.value);
      }
    });

    $("#lockBtn")?.addEventListener("click", async () => {
      try {
        await fetch(`${API}/lock`, { method: "POST" });
      } catch (error) {
        /* locking locally is still correct */
      }
      toast(t("toast.locked"));
      showGate("unlock");
    });

    $("#railToggle")?.addEventListener("click", () => {
      if ($("#app")?.classList.contains("rail-collapsed")) {
        setRailCollapsed(false);
        return;
      }
      toggleRail();
    });
    $("#scrim")?.addEventListener("click", closeRail);

    $("#railCollapse")?.addEventListener("click", () => setRailCollapsed(true));

    $("#railSearch")?.addEventListener("click", () => {
      const row = $("#searchRow");
      const button = $("#railSearch");
      if (!row) return;
      const show = row.hidden;
      row.hidden = !show;
      button?.classList.toggle("active", show);
      if (show) $("#searchInput")?.focus();
      else if ($("#searchInput")) $("#searchInput").blur();
    });

    $("#meBtn")?.addEventListener("click", openSettings);

    $("#callBtn")?.addEventListener("click", startCall);
    $("#callEnd")?.addEventListener("click", endCall);
    $("#callMic")?.addEventListener("click", () => {
      if (!call.active) return;
      if (call.listening || call.wantListen) {
        stopListening();
        callHint(t("call.hint"));
        callStatus(t("call.live", { time: callClock(call.seconds) }));
        return;
      }
      listen();
    });

    $("#callForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const field = $("#callInput");
      const message = (field?.value || "").trim();
      if (!message || !call.active) return;
      if (field) field.value = "";
      stopListening();
      stopSpeaking();
      callLine("out", t("call.heard", { text: message }));
      callStatus(t("call.thinking"));
      const reply = await send(message);
      if (!call.active) return;
      if (reply) {
        callLine("in", t("call.reply", { text: reply }));
        speak(reply);
      } else {
        callStatus(t("call.live", { time: callClock(call.seconds) }));
      }
    });

    /* --- another device --- */
    $("#gatePairOpen")?.addEventListener("click", () => openPairForm($("#gatePair")?.hidden !== false));
    $("#pairCancel")?.addEventListener("click", () => openPairForm(false));
    $("#pairGo")?.addEventListener("click", pairThisDevice);
    $("#pairCode")?.addEventListener("input", (event) => {
      event.target.value = event.target.value.replace(/\D/g, "").slice(0, 6);
    });
    $("#pairCode")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        pairThisDevice();
      }
    });

    $("#devCode")?.addEventListener("click", requestPairCode);
    $("#devCopy")?.addEventListener("click", async () => {
      const text = state.devUrls[0] || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        toast(t("set.devCopied"));
      } catch (error) {
        toast(text);
      }
    });
    $("#devList")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-device]");
      if (button) removeDevice(button.dataset.device);
    });
    $("#call")?.addEventListener("click", (event) => {
      if (event.target === $("#call")) endCall();
    });

    /* --- settings --- */
    $("#settingsBtn")?.addEventListener("click", openSettings);
    $("#settingsClose")?.addEventListener("click", closeSettings);
    $("#settings")?.addEventListener("click", (event) => {
      if (event.target === $("#settings")) closeSettings();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if ($("#call") && !$("#call").hidden) {
        endCall();
        return;
      }
      if ($("#settings") && !$("#settings").hidden) closeSettings();
    });

    $$("#themeGrid .theme").forEach((button) => {
      button.addEventListener("click", () => {
        const theme = applyTheme(button.dataset.theme);
        saveSettings({ theme });
      });
    });

    $$("#setLang button").forEach((button) => {
      button.addEventListener("click", () => {
        setLanguage(button.dataset.lang);
        paintSettings();
        $$("#setLang button").forEach((other) => {
          other.classList.toggle("active", other.dataset.lang === state.lang);
        });
      });
    });

    $$(".snav").forEach((button) => {
      button.addEventListener("click", () => openPane(button.dataset.pane));
    });

    $("#renameSave")?.addEventListener("click", saveRename);
    $("#renameCancel")?.addEventListener("click", closeRename);
    $("#renameInput")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        saveRename();
      }
      if (event.key === "Escape") closeRename();
    });
    $("#renameSheet")?.addEventListener("click", (event) => {
      if (event.target === $("#renameSheet")) closeRename();
    });

    $("#threadEdit")?.addEventListener("click", () => {
      if (state.activeId !== null) openRename(state.activeId);
    });

    $("#nameInput")?.addEventListener("change", (event) => {
      const name = event.target.value.trim();
      if (name) saveSettings({ name });
    });

    $("#pinReveal")?.addEventListener("click", () => {
      state.pinVisible = !state.pinVisible;
      const dots = $("#pinDots");
      if (dots) dots.textContent = state.pinVisible ? dots.dataset.pin || "••••" : "••••";
      $("#pinReveal").textContent = state.pinVisible ? t("set.pinHide") : t("set.pinShow");
    });

    $("#emojiToggle")?.addEventListener("change", (event) => {
      saveSettings({ emoji: event.target.checked });
    });

    $("#keyAdd")?.addEventListener("click", async () => {
      const field = $("#keyInput");
      const key = (field?.value || "").trim();
      if (key.length < 8) return;
      try {
        const response = await api("/settings/key", {
          method: "POST",
          body: JSON.stringify({ key }),
        });
        if (response.status === 409) {
          toast(t("toast.keyExists"));
          return;
        }
        if (!response.ok) throw new Error("key");
        if (field) field.value = "";
        state.settings = { ...(state.settings || {}), keys: await response.json() };
        paintSettings();
        toast(t("toast.keyAdded"));
      } catch (error) {
        toast(t("toast.failed"));
      }
    });

    $("#avatarInput")?.addEventListener("change", async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      try {
        const response = await fetch(`${API}/settings/avatar`, {
          method: "POST",
          body: form,
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error("avatar");
        await loadSettings();
        toast(t("toast.avatar"));
      } catch (error) {
        toast(t("toast.avatarFailed"));
      }
    });

    $("#avatarRemove")?.addEventListener("click", async () => {
      try {
        await api("/settings/avatar", { method: "DELETE" });
        await loadSettings();
      } catch (error) {
        toast(t("toast.failed"));
      }
    });

    $("#gateAccount")?.addEventListener("click", () => {
      if (!state.pinSet) {
        showGate("setup");
        return;
      }
      gateMessage(t("gate.already"), "error");
    });
  };

  /* ---------- themes ---------- */
  const THEMES = ["obsidian", "midnight", "emerald", "sepia", "violet", "graphite"];

  const applyTheme = (theme) => {
    const name = THEMES.includes(theme) ? theme : "obsidian";
    document.documentElement.dataset.theme = name;
    $$("#themeGrid .theme").forEach((button) => {
      button.classList.toggle("active", button.dataset.theme === name);
    });
    try {
      window.localStorage.setItem("ksenos-theme", name);
    } catch (error) {
      /* private mode */
    }
    return name;
  };

  const storedTheme = () => {
    try {
      return window.localStorage.getItem("ksenos-theme") || "obsidian";
    } catch (error) {
      return "obsidian";
    }
  };

  /* ---------- settings ---------- */
  const paintSettings = () => {
    const data = state.settings;
    if (!data) return;

    const name = data.name || "Ksenos";
    const nameInput = $("#nameInput");
    if (nameInput && document.activeElement !== nameInput) nameInput.value = name;

    const initial = $("#profileInitial");
    const image = $("#profileImg");
    if (initial) initial.textContent = (name[0] || "K").toUpperCase();
    if (image) {
      if (data.avatar) {
        image.src = `${API}/avatar?v=${encodeURIComponent(data.avatar)}`;
        image.hidden = false;
        if (initial) initial.hidden = true;
      } else {
        image.hidden = true;
        if (initial) initial.hidden = false;
      }
    }

    applyTheme(data.theme || storedTheme());

    $$("#setLang button").forEach((button) => {
      button.classList.toggle("active", button.dataset.lang === state.lang);
    });

    const emoji = $("#emojiToggle");
    if (emoji) emoji.checked = Boolean(data.emoji);

    const dots = $("#pinDots");
    if (dots) {
      dots.dataset.pin = data.pin || "";
      dots.textContent = state.pinVisible && data.pin ? data.pin : "••••";
    }
    const reveal = $("#pinReveal");
    if (reveal) {
      reveal.disabled = !data.pin;
      reveal.textContent = state.pinVisible ? t("set.pinHide") : t("set.pinShow");
      reveal.title = data.pin ? "" : t("set.pinUnavailable");
    }

    const lockNote = $("#lockNote");
    if (lockNote) {
      lockNote.textContent = t("set.pinLock", {
        n: data.max_fails || 5,
        m: Math.round((15 * 60) / 60),
      });
    }

    const keyCount = $("#keyCount");
    if (keyCount) {
      keyCount.textContent = t("set.keys", { n: data.keys ? data.keys.count : 0 });
    }

    paintIdentity(name, data.avatar);
  };

  /* The same name and picture appear in the rail footer, the chat head
     and the call screen, so they are painted from one place. */
  const paintIdentity = (name, avatar) => {
    const initial = (name[0] || "K").toUpperCase();
    const src = avatar ? `${API}/avatar?v=${encodeURIComponent(avatar)}` : "";

    const pairs = [
      ["#meImg", "#meInitial"],
      ["#callImg", "#callInitial"],
    ];
    pairs.forEach(([imgId, initialId]) => {
      const img = $(imgId);
      const letter = $(initialId);
      if (img) {
        if (src) {
          img.src = src;
          img.hidden = false;
          if (letter) letter.hidden = true;
        } else {
          img.hidden = true;
          if (letter) letter.hidden = false;
        }
      }
      if (letter) letter.textContent = initial;
    });

    const meName = $("#meName");
    if (meName) meName.textContent = name;
    const callName = $("#callName");
    if (callName) callName.textContent = name;
    const chatName = $("#chatName");
    if (chatName) chatName.textContent = name;
  };

  const loadSettings = async () => {
    try {
      const response = await api("/settings");
      if (!response.ok) return;
      state.settings = await response.json();
      applyTheme(state.settings.theme || storedTheme());
      paintSettings();
    } catch (error) {
      /* settings never block the chat */
    }
  };

  const saveSettings = async (changes) => {
    try {
      const response = await api("/settings", {
        method: "POST",
        body: JSON.stringify(changes),
      });
      if (!response.ok) throw new Error("save");
      state.settings = await response.json();
      paintSettings();
      toast(t("toast.saved"));
    } catch (error) {
      toast(t("toast.failed"));
    }
  };

  const openPane = (name) => {
    $$(".snav").forEach((button) => {
      button.classList.toggle("active", button.dataset.pane === name);
    });
    $$(".pane").forEach((pane) => {
      pane.classList.toggle("active", pane.dataset.pane === name);
    });
    $(".sheet-content")?.scrollTo({ top: 0, behavior: "smooth" });
    if (name === "devices") loadDevices();
  };

  const openSettings = () => {
    const sheet = $("#settings");
    if (!sheet) return;
    state.pinVisible = false;
    openPane("profile");
    const counts = $("#dataCounts");
    if (counts) {
      counts.textContent = t("data.summary", {
        c: state.conversations.length,
        m: state.conversations.reduce((sum, item) => sum + (item.message_count || 0), 0),
      });
    }
    sheet.hidden = false;
    loadSettings();
    paintSettings();
  };

  const closeSettings = () => {
    const sheet = $("#settings");
    if (sheet) sheet.hidden = true;
  };

  /* ---------- renaming a conversation ---------- */
  let renameId = null;

  const openRename = (id) => {
    const found = state.conversations.find((item) => item.id === id);
    if (!found) return;
    renameId = id;
    const sheet = $("#renameSheet");
    const field = $("#renameInput");
    if (field) {
      field.value = found.title || "";
    }
    if (sheet) sheet.hidden = false;
    window.setTimeout(() => {
      field?.focus();
      field?.select();
    }, 60);
  };

  const closeRename = () => {
    const sheet = $("#renameSheet");
    if (sheet) sheet.hidden = true;
    renameId = null;
  };

  const saveRename = async () => {
    if (renameId === null) return;
    const field = $("#renameInput");
    const title = (field?.value || "").trim();
    if (!title) return;
    try {
      const response = await api(`/history/${renameId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      if (!response.ok) throw new Error("rename");
      const data = await response.json();
      state.conversations = state.conversations.map((item) =>
        item.id === renameId ? { ...item, title: data.title } : item
      );
      if (state.activeId === renameId) {
        const badge = $("#threadBadge");
        if (badge) {
          badge.textContent = data.title;
          badge.hidden = false;
        }
      }
      renderHistory();
      closeRename();
      toast(t("toast.renamed"));
    } catch (error) {
      toast(t("toast.renameFailed"));
    }
  };

  /* ---------- keypad lockout ---------- */
  const stopLockout = () => {
    window.clearInterval(state.lockTimer);
    state.lockTimer = null;
  };

  const startLockout = (seconds) => {
    stopLockout();
    let left = Number(seconds) || 0;
    if (left <= 0) return;
    const card = $("#gateCard");
    const tick = () => {
      const minutes = Math.floor(left / 60);
      const rest = String(left % 60).padStart(2, "0");
      gateMessage(t("gate.locked", { time: `${minutes}:${rest}` }), "error");
      if (card) card.classList.add("locked");
      if (left <= 0) {
        stopLockout();
        if (card) card.classList.remove("locked");
        gateMessage("");
        return;
      }
      left -= 1;
    };
    tick();
    state.lockTimer = window.setInterval(tick, 1000);
  };

  /* ---------- reminders that fire while the window is open ---------- */
  const INBOX_KEY = "ksenos-inbox-last";

  const inboxNumber = () => {
    try {
      return Number(window.localStorage.getItem(INBOX_KEY) || 0) || 0;
    } catch (error) {
      return 0;
    }
  };

  const rememberInbox = (value) => {
    try {
      window.localStorage.setItem(INBOX_KEY, String(value));
    } catch (error) {
      /* private mode: the next poll may repeat one notice */
    }
  };

  const showReminder = (item) => {
    const text = (item && item.text) || "";
    if (!text) return;
    if (item.kind === "call") {
      toast(t("call.ringing"));
      if (!call.active) startCall();
      window.setTimeout(() => {
        callLine("system", t("call.ringing"));
        callLine("system", text);
        speak(text);
      }, 800);
      return;
    }
    if ($("#emptyState")) {
      thread.innerHTML = "";
      renderedDay = null;
    }
    appendMessage("agent", `🔔 ${text}`);
    toast(text.length > 80 ? `${text.slice(0, 77)}…` : text);
    if (!document.hidden) speak(text);
  };

  const pollInbox = async () => {
    if (!state.unlocked) return;
    try {
      const response = await api(`/inbox?after=${inboxNumber()}`);
      if (!response.ok) return;
      const data = await response.json();
      const items = data.items || [];
      // On the first poll only the newest few are shown: older ones were
      // already missed and must not bury the conversation in notices.
      (state.inboxReady ? items : items.slice(-3)).forEach(showReminder);
      state.inboxReady = true;
      if (data.last) rememberInbox(data.last);
    } catch (error) {
      /* a missed poll is retried on the next tick */
    }
  };

  const startInbox = () => {
    if (state.inboxTimer) return;
    pollInbox();
    state.inboxTimer = window.setInterval(pollInbox, 15000);
  };

  /* ---------- connected devices ---------- */
  const paintDevices = () => {
    const urls = $("#devUrls");
    if (urls) urls.textContent = state.devUrls.length ? state.devUrls.join("  ·  ") : "—";

    const note = $("#devLanNote");
    if (note) {
      const local = !state.devLanHost || state.devLanHost === "127.0.0.1" || state.devLanHost === "localhost" || state.devLanHost === "::1";
      note.textContent = local ? t("set.devLanOff") : t("set.devLanOn");
    }

    const list = $("#devList");
    if (!list) return;
    if (!state.devices.length) {
      list.innerHTML = `<div class="dev-empty">${escapeHtml(t("set.devEmpty"))}</div>`;
      return;
    }
    list.innerHTML = state.devices
      .map((device) => {
        const seen = device.last_seen
          ? `${shortDate(device.last_seen)} ${clock(device.last_seen)}`
          : "—";
        const name = `${device.name || "Qurilma"}${device.current ? ` · ${t("set.devThis")}` : ""}`;
        return `
          <div class="dev-item">
            <span class="dev-id">
              <b>${escapeHtml(name)}</b>
              <small>${escapeHtml(t("set.devSeen", { when: seen }))}</small>
            </span>
            <button class="btn-line danger" type="button" data-device="${escapeHtml(device.id)}">${escapeHtml(t("set.devRemove"))}</button>
          </div>`;
      })
      .join("");
  };

  const loadDevices = async () => {
    try {
      const response = await api("/devices");
      if (!response.ok) return;
      const data = await response.json();
      state.devUrls = data.urls || [];
      state.devLanHost = data.lan_host || "127.0.0.1";
      state.devices = data.devices || [];
      paintDevices();
    } catch (error) {
      /* the pane simply stays as it was */
    }
  };

  const requestPairCode = async () => {
    try {
      const response = await api("/devices/code", { method: "POST" });
      if (!response.ok) throw new Error("code");
      const data = await response.json();
      if (data.urls && data.urls.length) state.devUrls = data.urls;
      if (data.lan_host) state.devLanHost = data.lan_host;
      paintDevices();

      const number = $("#pairNumber");
      if (number) number.textContent = data.code || "------";
      const box = $("#pairBox");
      if (box) box.hidden = false;
      state.pairExpires = Date.now() + Number(data.expires_in || 600) * 1000;

      window.clearInterval(state.pairTimer);
      const tick = () => {
        const left = Math.max(0, Math.round((state.pairExpires - Date.now()) / 1000));
        const label = $("#pairLeft");
        if (label) label.textContent = left ? t("set.devLeft", { m: Math.ceil(left / 60) }) : t("gate.pairWrong");
        if (!left) {
          window.clearInterval(state.pairTimer);
          state.pairTimer = null;
          if (box) box.hidden = true;
        }
      };
      tick();
      state.pairTimer = window.setInterval(tick, 5000);
    } catch (error) {
      toast(t("toast.failed"));
    }
  };

  const removeDevice = async (id) => {
    try {
      const response = await api(`/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("device");
      await loadDevices();
    } catch (error) {
      toast(t("toast.failed"));
    }
  };

  /* ---------- boot ---------- */
  const setRailCollapsed = (collapsed) => {
    $("#app")?.classList.toggle("rail-collapsed", collapsed);
    try {
      window.localStorage.setItem("ksenos-rail-collapsed", collapsed ? "1" : "0");
    } catch (error) {
      /* private mode */
    }
  };

  const storedRail = () => {
    try {
      return window.localStorage.getItem("ksenos-rail-collapsed") === "1";
    } catch (error) {
      return false;
    }
  };

  const boot = async () => {
    state.lang = detectLang();
    applyLanguage();
    applyTheme(storedTheme());
    setRailCollapsed(storedRail());
    bind();

    try {
      const response = await fetch(`${API}/status`, { credentials: "same-origin" });
      if (!response.ok) throw new Error("status");
      const data = await response.json();
      state.pinSet = Boolean(data.pin_set);
      const version = $("#aboutVersion");
      if (version && data.version) version.textContent = data.version;
      if (data.lock_seconds) startLockout(data.lock_seconds);
      if (data.unlocked) {
        openApp();
      } else {
        showGate("unlock");
      }
    } catch (error) {
      showGate("unlock");
      gateMessage(t("gate.offline"), "error");
      const keypad = $("#keypad");
      if (keypad) keypad.style.opacity = "0.4";
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
