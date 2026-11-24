/* ============================================================
   Ksenos Messenger — three languages (Uzbek / Russian / English)
   The page picks the visitor's language from the browser settings
   and the timezone (i.e. the country), then remembers a manual pick.
   ============================================================ */

(() => {
  "use strict";

  const STRINGS = {
    uz: {
      "meta.title": "Ksenos Messenger — Windows uchun shaxsiy chat",
      "meta.description":
        "Ksenos Messenger — Windows uchun yengil chat. Yozing yoki qoʻngʻiroq qiling: kontaktlar yoʻq, faqat siz va AI. Ish stolida jonli robot hamroh.",

      "nav.tour": "Koʻrinish",
      "nav.why": "Nega Ksenos",
      "nav.pin": "Kirish kodi",
      "nav.roadmap": "Reja",
      "nav.download": "Yuklab olish",
      "nav.version": "v1.0 · Windows",
      "nav.aria": "Boʻlimlar",
      "nav.linksAria": "Havolalar",

      "hero.eyebrow": "Ksenos Messenger — Windows 10 / 11",
      "hero.h1a": "Yozing.",
      "hero.h1b": "Gapiring.",
      "hero.h1c": "U yoningizda.",
      "hero.lede":
        "Kontaktlar yoʻq, guruhlar yoʻq, ortiqcha hech narsa yoʻq. Kompyuteringizda faqat siz va sizning AI yordamchingiz — yozishadi, gaplashadi va ish stolida birga yuradi.",

      "cta.download": "Yuklab olish",
      "cta.badge": "bepul",
      "cta.tour": "Koʻrinishni koʻrish",

      "fine.size": "≈3 MB",
      "fine.exe": ".exe oʻrnatuvchi",
      "fine.install": "oʻrnatish 1 daqiqa",

      "chat.online": "online · faqat agent",
      "chat.today": "Bugun",
      "chat.m1": "Salom! Reja tayyormi?",
      "chat.m2": "Ertangi uchrashuvni eslatib qoʻy.",
      "chat.m3": "Yozib qoʻydim — ertaga 9:00 da eslataman.",
      "chat.composer": "Xabar yozing…",

      "tag.pin": "4 xonali kod",
      "tag.pinSub": "oʻzgarmaydi",
      "tag.local": "Mahalliy",
      "tag.localSub": "bulutsiz",

      "cue.down": "Pastga",
      "cue.label": "Pastga",

      "ticker.typing": "Yozish",
      "ticker.call": "Ovozli qoʻngʻiroq",
      "ticker.robot": "Ish stolidagi robot",
      "ticker.pin": "4 xonali kod",
      "ticker.chat": "Bitta suhbat",
      "ticker.local": "Mahalliy ishlash",

      "why.label": "Nega Ksenos",
      "why.h2a": "Telefondagi messenjer emas.",
      "why.h2b": "Kompyuteringizdagi",
      "why.h2c": "hamroh.",
      "why.lead":
        "Har bir detal bitta maqsad uchun qurilgan: siz va AI oʻrtasida hech qanday ortiqcha qatlam boʻlmasin.",
      "why.r1t": "Bitta suhbat. Boshqa hech kim.",
      "why.r1p":
        "Kontaktlar, qabul qiluvchi maydoni va guruhlar yoʻq. Xabar faqat shu kompyuterdagi Ksenosʼga boradi.",
      "why.r2t": "Yozish ham, gapirish ham.",
      "why.r2p":
        "Mikrofonni tanlab gapirasiz — Ksenos eshitadi va ovoz bilan javob beradi. Dinamikdan chiqadi, telefonsiz.",
      "why.r3t": "Ish stolida jonli robot.",
      "why.r3p":
        "Ekranda erkin yuradigan, sakraydigan va holatingizga javob beradigan hamroh — kadrlari tiniq, harakat silliq.",
      "why.r4t": "Hammasi shu kompyuterda.",
      "why.r4p":
        "Server mahalliy ishlaydi: suhbat, eslatmalar va ovoz tashqariga chiqmaydi. Reklama ham, kuzatuv ham yoʻq.",

      "tour.label": "Koʻrinish",
      "tour.h2a": "Uchta ekran.",
      "tour.h2b": "Bitta odam.",
      "tour.tabsAria": "Koʻrinishlar",
      "tour.tabChat": "Chat",
      "tour.tabCall": "Qoʻngʻiroq",
      "tour.tabRobot": "Hamroh",
      "tour.m1": "Bugun nima qilamiz?",
      "tour.m2": "Kechqurun hisobotni tayyorlab ber.",
      "tour.m3": "Tayyorlayman. Faylni qayerga saqlay?",
      "tour.m4": "Ish stoliga.",
      "tour.m5": "Saqladim — “hisobot.xlsx”. Yana biror narsa?",
      "tour.callStatus": "Qoʻngʻiroq · 00:24",
      "tour.callHint": "Gapiring — eshityapman",
      "tour.bubble": "Men shu yerdaman — bir chaqiriq uzoqda.",
      "tour.caption": "Ish stoli ustida erkin yuradi, ishlashingizga xalaqit bermaydi.",
      "tour.titleChat": "Ksenos Messenger — Chat",
      "tour.titleCall": "Ksenos Messenger — Qoʻngʻiroq",
      "tour.titleRobot": "Ksenos Messenger — Hamroh",

      "pin.label": "Keyingi versiya",
      "pin.h2a": "Bitta kod.",
      "pin.h2b": "Har qanday kompyuter.",
      "pin.lead":
        "Mini ilovani yangi kompyuterga yuklasangiz ham suhbatingiz siz bilan qoladi. Oʻrnatilgandan soʻng 4 xonali maxsus parol beriladi — u hech qachon oʻzgarmaydi. PIN kodni kiritib, oʻz akkauntingizga kirasiz.",
      "pin.p1t": "Doimiy kod",
      "pin.p1p": "Bir marta beriladi va umuman oʻzgarmaydi.",
      "pin.p2t": "PIN bilan kirish",
      "pin.p2p": "Kodni yozasiz — akkaunt oʻzi ochiladi.",
      "pin.p3t": "Bir akkaunt",
      "pin.p3p": "Suhbat, eslatmalar va sozlamalar bir joyda.",
      "pin.badge": "Reja",
      "pin.title": "Kirish kodi",
      "pin.sub": "Akkauntingizga kirish uchun 4 xonali kodni kiriting",
      "pin.slotsAria": "PIN kod",
      "pin.hint": "Demo koʻrinishi · kod tekshirilmaydi",
      "pin.done": "Kod toʻliq kiritildi · akkaunt ochilmoqda…",
      "pin.restart": "Tayyor. Demo qaytadan boshlandi.",

      "road.label": "Kelajakdagi reja",
      "road.h2a": "Qadam-baqadam,",
      "road.h2b": "oshkora.",
      "road.lead": "Nima tayyor, nima ustida ishlanmoqda — hammasi shu yerda.",
      "road.stateDone": "Tayyor",
      "road.stateNext": "Keyingi",
      "road.statePlan": "Kelajakda",
      "road.c1t": "Messenger va robot",
      "road.c1p": "Windows oynasi, ovozli qoʻngʻiroq va jonli hamroh — ishlayapti.",
      "road.c2t": "Yuklab olish sayti",
      "road.c2p": "Aynan shu sahifa: bitta tugma, bitta oʻrnatuvchi, bitta bosish.",
      "road.c3t": "4 xonali PIN",
      "road.c3p": "Oʻzgarmas kod bilan akkauntga kirish.",
      "road.c4t": "Boshqa kompyuter",
      "road.c4p": "PIN kodni kiritib, suhbatni yangi qurilmada davom ettirasiz.",

      "final.eyebrow": "Bepul · v1.0",
      "final.h2a": "Bugun oʻrnating.",
      "final.h2b": "Ertaga birga ishlaymiz.",
      "final.btn": "Ksenos Messengerʼni yuklab olish",
      "final.n1": "Windows 10 / 11",
      "final.n2": "64-bit",
      "final.n3": "1 daqiqa",

      "foot.note":
        "Shaxsiy AI yordamchi uchun yengil Windows chat. Maʼlumotlaringiz shu kompyuterda qoladi.",

      "dl.preparing": "Fayl tayyorlanmoqda — bir necha soniya…",
      "dl.label": "Yuklab olish",
      "dl.badge": "bepul",
      "dl.doneLabel": "Yuklandi",
      "dl.doneNote": "Oʻrnatuvchini ishga tushiring — bir daqiqada tayyor.",
      "dl.toast": "Tayyor · {size} yuklandi.",
      "dl.fallbackNote":
        "Brauzer yuklamoqda… Sahifani server orqali ochganingizga ishonch hosil qiling.",
      "dl.fallbackToast": "Yuklash boshlandi. Fayl «Yuklamalar» jildiga tushadi.",
      "dl.errorNote": "Yuklab boʻlmadi. Qaytadan urinib koʻring.",
    },

    ru: {
      "meta.title": "Ksenos Messenger — личный чат для Windows",
      "meta.description":
        "Ksenos Messenger — лёгкий чат для Windows. Пишите или звоните: контактов нет, только вы и ИИ. Живой робот-компаньон на рабочем столе.",

      "nav.tour": "Обзор",
      "nav.why": "Почему Ksenos",
      "nav.pin": "Код доступа",
      "nav.roadmap": "План",
      "nav.download": "Скачать",
      "nav.version": "v1.0 · Windows",
      "nav.aria": "Разделы",
      "nav.linksAria": "Ссылки",

      "hero.eyebrow": "Ksenos Messenger — Windows 10 / 11",
      "hero.h1a": "Пишите.",
      "hero.h1b": "Говорите.",
      "hero.h1c": "Он рядом.",
      "hero.lede":
        "Ни контактов, ни групп, ни лишнего — ничего. На вашем компьютере только вы и ваш ИИ-помощник: он пишет, говорит и гуляет вместе с вами по рабочему столу.",

      "cta.download": "Скачать",
      "cta.badge": "бесплатно",
      "cta.tour": "Посмотреть обзор",

      "fine.size": "≈3 MB",
      "fine.exe": ".exe установщик",
      "fine.install": "установка за 1 минуту",

      "chat.online": "online · только агент",
      "chat.today": "Сегодня",
      "chat.m1": "Привет! План готов?",
      "chat.m2": "Напомни о завтрашней встрече.",
      "chat.m3": "Записал — напомню завтра в 9:00.",
      "chat.composer": "Напишите сообщение…",

      "tag.pin": "4-значный код",
      "tag.pinSub": "не меняется",
      "tag.local": "Локально",
      "tag.localSub": "без облака",

      "cue.down": "Вниз",
      "cue.label": "Вниз",

      "ticker.typing": "Переписка",
      "ticker.call": "Голосовой звонок",
      "ticker.robot": "Робот на рабочем столе",
      "ticker.pin": "4-значный код",
      "ticker.chat": "Один диалог",
      "ticker.local": "Работает локально",

      "why.label": "Почему Ksenos",
      "why.h2a": "Не мессенджер в телефоне.",
      "why.h2b": "Компаньон",
      "why.h2c": "на вашем компьютере.",
      "why.lead":
        "Каждая деталь сделана ради одной цели: между вами и ИИ не должно быть лишних слоёв.",
      "why.r1t": "Один диалог. И больше никого.",
      "why.r1p":
        "Ни контактов, ни поля получателя, ни групп. Сообщение уходит только Ksenos на этом компьютере.",
      "why.r2t": "И писать, и говорить.",
      "why.r2p":
        "Выбираете микрофон и говорите — Ksenos слышит и отвечает голосом. Звук идёт из динамиков, без телефона.",
      "why.r3t": "Живой робот на рабочем столе.",
      "why.r3p":
        "Компаньон свободно ходит по экрану, прыгает и реагирует на вас — чёткие кадры, плавное движение.",
      "why.r4t": "Всё на этом компьютере.",
      "why.r4p":
        "Сервер работает локально: переписка, напоминания и голос не уходят наружу. Ни рекламы, ни слежки.",

      "tour.label": "Обзор",
      "tour.h2a": "Три экрана.",
      "tour.h2b": "Один человек.",
      "tour.tabsAria": "Экраны",
      "tour.tabChat": "Чат",
      "tour.tabCall": "Звонок",
      "tour.tabRobot": "Компаньон",
      "tour.m1": "Чем займёмся сегодня?",
      "tour.m2": "Подготовь отчёт к вечеру.",
      "tour.m3": "Готовлю. Куда сохранить файл?",
      "tour.m4": "На рабочий стол.",
      "tour.m5": "Сохранил — «отчёт.xlsx». Что-то ещё?",
      "tour.callStatus": "Звонок · 00:24",
      "tour.callHint": "Говорите — я слушаю",
      "tour.bubble": "Я здесь — в одном вызове.",
      "tour.caption": "Свободно гуляет по рабочему столу и не мешает работе.",
      "tour.titleChat": "Ksenos Messenger — Чат",
      "tour.titleCall": "Ksenos Messenger — Звонок",
      "tour.titleRobot": "Ksenos Messenger — Компаньон",

      "pin.label": "Следующая версия",
      "pin.h2a": "Один код.",
      "pin.h2b": "Любой компьютер.",
      "pin.lead":
        "Даже если вы поставите мини-приложение на новый компьютер, переписка останется с вами. После установки выдаётся 4-значный пароль — он не меняется никогда. Вводите PIN и входите в свой аккаунт.",
      "pin.p1t": "Постоянный код",
      "pin.p1p": "Выдаётся один раз и не меняется.",
      "pin.p2t": "Вход по PIN",
      "pin.p2p": "Вводите код — аккаунт открывается сам.",
      "pin.p3t": "Один аккаунт",
      "pin.p3p": "Переписка, напоминания и настройки в одном месте.",
      "pin.badge": "План",
      "pin.title": "Код доступа",
      "pin.sub": "Введите 4-значный код, чтобы войти в аккаунт",
      "pin.slotsAria": "PIN-код",
      "pin.hint": "Демо · код не проверяется",
      "pin.done": "Код введён · аккаунт открывается…",
      "pin.restart": "Готово. Демо началось заново.",

      "road.label": "План развития",
      "road.h2a": "Шаг за шагом,",
      "road.h2b": "открыто.",
      "road.lead": "Что готово, а что в работе — всё здесь.",
      "road.stateDone": "Готово",
      "road.stateNext": "Далее",
      "road.statePlan": "В будущем",
      "road.c1t": "Мессенджер и робот",
      "road.c1p": "Окно Windows, голосовой звонок и живой компаньон — работает.",
      "road.c2t": "Сайт для скачивания",
      "road.c2p": "Эта страница: одна кнопка, один установщик, одно нажатие.",
      "road.c3t": "4-значный PIN",
      "road.c3p": "Вход в аккаунт по постоянному коду.",
      "road.c4t": "Другой компьютер",
      "road.c4p": "Вводите PIN и продолжаете переписку на новом устройстве.",

      "final.eyebrow": "Бесплатно · v1.0",
      "final.h2a": "Установите сегодня.",
      "final.h2b": "Завтра работаем вместе.",
      "final.btn": "Скачать Ksenos Messenger",
      "final.n1": "Windows 10 / 11",
      "final.n2": "64-bit",
      "final.n3": "1 минута",

      "foot.note":
        "Лёгкий Windows-чат для личного ИИ-помощника. Ваши данные остаются на этом компьютере.",

      "dl.preparing": "Готовим файл — пара секунд…",
      "dl.label": "Скачать",
      "dl.badge": "бесплатно",
      "dl.doneLabel": "Скачано",
      "dl.doneNote": "Запустите установщик — через минуту всё готово.",
      "dl.toast": "Готово · загружено {size}.",
      "dl.fallbackNote":
        "Браузер скачивает… Убедитесь, что страница открыта с сервера.",
      "dl.fallbackToast": "Загрузка началась. Файл появится в «Загрузках».",
      "dl.errorNote": "Скачать не удалось. Попробуйте ещё раз.",
    },

    en: {
      "meta.title": "Ksenos Messenger — a private chat for Windows",
      "meta.description":
        "Ksenos Messenger — a light Windows chat. Type or call: no contacts, just you and your AI. A living robot companion on your desktop.",

      "nav.tour": "Tour",
      "nav.why": "Why Ksenos",
      "nav.pin": "Access code",
      "nav.roadmap": "Roadmap",
      "nav.download": "Download",
      "nav.version": "v1.0 · Windows",
      "nav.aria": "Sections",
      "nav.linksAria": "Links",

      "hero.eyebrow": "Ksenos Messenger — Windows 10 / 11",
      "hero.h1a": "Type.",
      "hero.h1b": "Speak.",
      "hero.h1c": "It sits beside you.",
      "hero.lede":
        "No contacts, no groups, nothing extra. Just you and your AI assistant on your computer — it writes, talks and walks along with you on the desktop.",

      "cta.download": "Download",
      "cta.badge": "free",
      "cta.tour": "See the tour",

      "fine.size": "≈3 MB",
      "fine.exe": ".exe installer",
      "fine.install": "1 minute to install",

      "chat.online": "online · agent only",
      "chat.today": "Today",
      "chat.m1": "Hi! Is the plan ready?",
      "chat.m2": "Remind me about tomorrow's meeting.",
      "chat.m3": "Noted — I'll remind you at 9:00 tomorrow.",
      "chat.composer": "Write a message…",

      "tag.pin": "4-digit code",
      "tag.pinSub": "never changes",
      "tag.local": "Local",
      "tag.localSub": "no cloud",

      "cue.down": "Scroll down",
      "cue.label": "Down",

      "ticker.typing": "Typing",
      "ticker.call": "Voice call",
      "ticker.robot": "Robot on the desktop",
      "ticker.pin": "4-digit code",
      "ticker.chat": "One conversation",
      "ticker.local": "Runs locally",

      "why.label": "Why Ksenos",
      "why.h2a": "Not a phone messenger.",
      "why.h2b": "A companion",
      "why.h2c": "on your own computer.",
      "why.lead":
        "Every detail serves one goal: no extra layer between you and the AI.",
      "why.r1t": "One conversation. Nobody else.",
      "why.r1p":
        "No contacts, no recipient field, no groups. A message goes only to Ksenos on this computer.",
      "why.r2t": "Write it, or say it.",
      "why.r2p":
        "Pick a microphone and speak — Ksenos hears you and answers with a voice. It comes out of the speakers, no phone needed.",
      "why.r3t": "A living robot on your desktop.",
      "why.r3p":
        "A companion that walks freely across the screen, jumps and reacts to you — crisp frames, smooth motion.",
      "why.r4t": "Everything stays on this computer.",
      "why.r4p":
        "The server runs locally: chat, reminders and voice never leave the machine. No ads, no tracking.",

      "tour.label": "Tour",
      "tour.h2a": "Three screens.",
      "tour.h2b": "One person.",
      "tour.tabsAria": "Screens",
      "tour.tabChat": "Chat",
      "tour.tabCall": "Call",
      "tour.tabRobot": "Companion",
      "tour.m1": "What are we doing today?",
      "tour.m2": "Prepare the report for tonight.",
      "tour.m3": "On it. Where should I save the file?",
      "tour.m4": "On the desktop.",
      "tour.m5": "Saved — “report.xlsx”. Anything else?",
      "tour.callStatus": "Call · 00:24",
      "tour.callHint": "Speak — I'm listening",
      "tour.bubble": "I'm right here — one call away.",
      "tour.caption": "Roams the desktop freely and never gets in the way.",
      "tour.titleChat": "Ksenos Messenger — Chat",
      "tour.titleCall": "Ksenos Messenger — Call",
      "tour.titleRobot": "Ksenos Messenger — Companion",

      "pin.label": "Next release",
      "pin.h2a": "One code.",
      "pin.h2b": "Any computer.",
      "pin.lead":
        "Even if you install the mini app on a new computer, your conversation stays with you. After setup you get a 4-digit password — it never changes. Enter the PIN and your account opens.",
      "pin.p1t": "Permanent code",
      "pin.p1p": "Issued once, never changed.",
      "pin.p2t": "Sign in with the PIN",
      "pin.p2p": "You type the code — the account opens itself.",
      "pin.p3t": "One account",
      "pin.p3p": "Chat, reminders and settings in one place.",
      "pin.badge": "Plan",
      "pin.title": "Access code",
      "pin.sub": "Enter the 4-digit code to open your account",
      "pin.slotsAria": "PIN code",
      "pin.hint": "Demo view · the code is not checked",
      "pin.done": "Code complete · opening your account…",
      "pin.restart": "Done. The demo started over.",

      "road.label": "Roadmap",
      "road.h2a": "Step by step,",
      "road.h2b": "in the open.",
      "road.lead": "What's finished and what's in progress — all of it here.",
      "road.stateDone": "Done",
      "road.stateNext": "Next",
      "road.statePlan": "Planned",
      "road.c1t": "Messenger and robot",
      "road.c1p": "A Windows window, voice calls and a living companion — working.",
      "road.c2t": "Download site",
      "road.c2p": "This very page: one button, one installer, one click.",
      "road.c3t": "4-digit PIN",
      "road.c3p": "Sign in to your account with a permanent code.",
      "road.c4t": "Another computer",
      "road.c4p": "Enter the PIN and continue the conversation on a new device.",

      "final.eyebrow": "Free · v1.0",
      "final.h2a": "Install it today.",
      "final.h2b": "Tomorrow we work together.",
      "final.btn": "Download Ksenos Messenger",
      "final.n1": "Windows 10 / 11",
      "final.n2": "64-bit",
      "final.n3": "1 minute",

      "foot.note":
        "A light Windows chat for your personal AI assistant. Your data stays on this computer.",

      "dl.preparing": "Preparing the file — a couple of seconds…",
      "dl.label": "Download",
      "dl.badge": "free",
      "dl.doneLabel": "Downloaded",
      "dl.doneNote": "Run the installer — you're ready in a minute.",
      "dl.toast": "Done · {size} downloaded.",
      "dl.fallbackNote":
        "Your browser is downloading… make sure the page is served by the app.",
      "dl.fallbackToast": "Download started. The file lands in your Downloads folder.",
      "dl.errorNote": "The download failed. Please try again.",
    },
  };

  const SUPPORTED = ["uz", "ru", "en"];
  const STORAGE_KEY = "ksenus-lang";

  const readStored = () => {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      return null;
    }
  };

  const writeStored = (value) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch (error) {
      /* private mode: the choice just does not persist */
    }
  };

  /* The visitor's language: their saved pick, then the browser's own
     language list, then the country guessed from the timezone. */
  const detect = () => {
    const saved = readStored();
    if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;

    const candidates = (navigator.languages && navigator.languages.length
      ? Array.from(navigator.languages)
      : [navigator.language || ""]
    ).map((code) => String(code).toLowerCase());

    for (const code of candidates) {
      if (code.indexOf("uz") === 0) return "uz";
      if (code.indexOf("ru") === 0) return "ru";
      if (code.indexOf("en") === 0) return "en";
    }

    let zone = "";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      zone = "";
    }

    if (/Tashkent|Samarkand|Bukhara|Uzbek/i.test(zone)) return "uz";
    if (/Moscow|Kiev|Minsk|Almaty|Bishkek|Tbilisi|Yerevan|Baku|Asia\//i.test(zone)) return "ru";
    return "en";
  };

  let lang = detect();

  const t = (key, vars) => {
    const table = STRINGS[lang] || STRINGS.uz;
    let text = table[key];
    if (text === undefined) text = STRINGS.uz[key];
    if (text === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach((name) => {
        text = text.split(`{${name}}`).join(String(vars[name]));
      });
    }
    return text;
  };

  const apply = (root = document) => {
    root.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-attr]").forEach((element) => {
      element.dataset.i18nAttr.split(",").forEach((pair) => {
        const parts = pair.split(":");
        if (parts.length !== 2) return;
        element.setAttribute(parts[0].trim(), t(parts[1].trim()));
      });
    });
    root.querySelectorAll("[data-i18n-aria]").forEach((element) => {
      element.setAttribute("aria-label", t(element.dataset.i18nAria));
    });
    document.documentElement.lang = lang === "uz" ? "uz-Latn" : lang;
  };

  const paintSwitch = () => {
    document.querySelectorAll("#langSwitch button").forEach((button) => {
      const active = button.dataset.lang === lang;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  };

  const setLang = (next, options) => {
    const persist = !options || options.persist !== false;
    if (SUPPORTED.indexOf(next) === -1) return;
    lang = next;
    if (persist) writeStored(next);
    apply();
    paintSwitch();
    document.dispatchEvent(new CustomEvent("ksenus:lang", { detail: { lang } }));
  };

  window.KsenosI18n = {
    get lang() {
      return lang;
    },
    t,
    apply,
    setLang,
    detect,
    languages: SUPPORTED.slice(),
  };

  const start = () => {
    apply();
    paintSwitch();
    document.querySelectorAll("#langSwitch button").forEach((button) => {
      button.addEventListener("click", () => setLang(button.dataset.lang));
    });
    document.dispatchEvent(new CustomEvent("ksenus:lang", { detail: { lang } }));
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
