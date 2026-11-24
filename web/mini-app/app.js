/* ============================================================
   Ksenos Messenger — page behaviour
   ============================================================ */

(() => {
  "use strict";

  const DOWNLOAD_URL = "/download/ksenos-setup.exe";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const t = (key, vars) => (window.KsenosI18n ? window.KsenosI18n.t(key, vars) : key);

  /* ---------- reading progress + sticky nav ---------- */
  const bar = $("#scrollBar");
  const nav = $("#nav");

  const onScroll = () => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    const share = max > 0 ? window.scrollY / max : 0;
    if (bar) bar.style.width = `${Math.min(100, share * 100)}%`;
    if (nav) nav.classList.toggle("scrolled", window.scrollY > 20);
  };

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  onScroll();

  /* ---------- scroll reveals ---------- */
  const revealables = $$(".reveal");

  if ("IntersectionObserver" in window && !reduceMotion) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.14, rootMargin: "0px 0px -6% 0px" }
    );
    revealables.forEach((el) => observer.observe(el));
  } else {
    revealables.forEach((el) => el.classList.add("in"));
  }

  /* ---------- sprite frames ---------- */
  const frame = (set, index) => `/mini-app/static/assets/${set}-${index}.png`;

  const animateSprite = (element, set, count, every) => {
    if (!element || reduceMotion) return;
    for (let i = 0; i < count; i += 1) {
      const preload = new Image();
      preload.src = frame(set, i);
    }
    let index = 0;
    window.setInterval(() => {
      index = (index + 1) % count;
      element.src = frame(set, index);
    }, every);
  };

  animateSprite($("#avatarSprite"), "idle", 4, 320);
  animateSprite($("#robotSprite"), "idle", 4, 260);

  /* ---------- product window tilt ---------- */
  const appWindow = $("#appWindow");
  const stage = $(".stage");

  if (appWindow && stage && !reduceMotion && window.matchMedia("(pointer: fine)").matches) {
    let queued = false;

    stage.addEventListener("pointermove", (event) => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        const box = stage.getBoundingClientRect();
        const dx = (event.clientX - box.left) / box.width - 0.5;
        const dy = (event.clientY - box.top) / box.height - 0.5;
        appWindow.style.transform =
          `rotateX(${1.6 - dy * 5}deg) rotateY(${-2.4 + dx * 7}deg) translateY(-4px)`;
      });
    });

    stage.addEventListener("pointerleave", () => {
      appWindow.style.transform = "";
    });
  }

  /* ---------- product tour tabs ---------- */
  const tabs = $$(".tab");
  const panels = $$(".tour-panel");
  const tourTitle = $("#tourTitle");
  const titles = () => ({
    chat: t("tour.titleChat"),
    call: t("tour.titleCall"),
    robot: t("tour.titleRobot"),
  });
  let currentTab = "chat";

  const selectTab = (name) => {
    currentTab = name;
    tabs.forEach((tab) => {
      const active = tab.dataset.panel === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
    const map = titles();
    if (tourTitle && map[name]) tourTitle.textContent = map[name];

    if (name === "robot") {
      const robot = $("#tourRobot");
      if (robot && !reduceMotion) {
        let index = 0;
        window.clearInterval(selectTab.robotTimer);
        selectTab.robotTimer = window.setInterval(() => {
          index = (index + 1) % 3;
          robot.src = frame("walk", index);
        }, 190);
      }
    } else {
      window.clearInterval(selectTab.robotTimer);
    }
  };

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.panel));
  });

  /* ---------- 4-digit PIN demo ---------- */
  const pinCard = $("#pinCard");
  const slots = $$("#slots .slot");
  const keypad = $("#keypad");
  const pinStatus = $("#pinStatus");
  const PIN_HINT = () => t("pin.hint");
  let pin = "";
  let pinLocked = false;

  const paintPin = () => {
    slots.forEach((slot, index) => {
      slot.textContent = pin[index] || "";
      slot.classList.toggle("filled", Boolean(pin[index]));
      slot.classList.toggle("active", index === pin.length && pin.length < 4);
    });
  };

  const resetPin = (message = PIN_HINT()) => {
    pin = "";
    pinLocked = false;
    pinCard?.classList.remove("ok", "bad");
    pinStatus?.classList.remove("ok");
    if (pinStatus) pinStatus.textContent = message;
    paintPin();
  };

  const completePin = () => {
    pinLocked = true;
    pinCard?.classList.add("ok");
    pinStatus?.classList.add("ok");
    if (pinStatus) pinStatus.textContent = t("pin.done");
    window.setTimeout(() => resetPin(t("pin.restart")), 1800);
    window.setTimeout(() => resetPin(), 3400);
  };

  const press = (key) => {
    if (!pinCard || pinLocked) return;
    if (key === "clear") {
      resetPin();
      return;
    }
    if (key === "back") {
      pin = pin.slice(0, -1);
      paintPin();
      return;
    }
    if (!/^\d$/.test(key) || pin.length >= 4) return;
    pin += key;
    paintPin();
    if (pin.length === 4) completePin();
  };

  keypad?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-key]");
    if (button) press(button.dataset.key);
  });

  document.addEventListener("keydown", (event) => {
    if (!pinCard) return;
    const box = pinCard.getBoundingClientRect();
    if (box.top > window.innerHeight * 0.9 || box.bottom < window.innerHeight * 0.1) return;
    if (/^\d$/.test(event.key)) press(event.key);
    else if (event.key === "Backspace") press("back");
    else if (event.key === "Escape") press("clear");
  });

  paintPin();

  /* ---------- download ---------- */
  const downloadBtn = $("#downloadBtn");
  const dlFill = $("#dlFill");
  const dlPct = $("#dlPct");
  const note = $("#downloadNote");
  const finalBtn = $("#finalBtn");
  const toast = $("#toast");
  const noteHTML = () =>
    `<span>${t("fine.size")}</span><i></i><span>${t("fine.exe")}</span><i></i><span>${t("fine.install")}</span>`;
  let downloading = false;

  const showToast = (message) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 5000);
  };

  const setProgress = (value) => {
    const percent = Math.max(0, Math.min(100, Math.round(value)));
    if (dlFill) dlFill.style.width = `${percent}%`;
    if (dlPct) dlPct.textContent = `${percent}%`;
  };

  const sizeText = (bytes) => {
    if (!bytes) return "";
    if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  };

  const saveBlob = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 8000);
  };

  const resetButton = () => {
    if (!downloadBtn) return;
    downloadBtn.classList.remove("loading", "done");
    const label = $(".dl-label", downloadBtn);
    const badge = $(".dl-size", downloadBtn);
    if (label) label.textContent = t("dl.label");
    if (badge) badge.textContent = t("dl.badge");
    setProgress(0);
  };

  const startDownload = async () => {
    if (!downloadBtn || downloading) return;
    downloading = true;
    downloadBtn.classList.remove("done");
    downloadBtn.classList.add("loading");
    setProgress(0);
    if (note) note.textContent = t("dl.preparing");

    try {
      const response = await fetch(DOWNLOAD_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const disposition = response.headers.get("content-disposition") || "";
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
      const filename = match ? decodeURIComponent(match[1]) : "Ksenos-Setup.exe";

      const total = Number(response.headers.get("content-length")) || 0;
      const reader = response.body?.getReader?.();
      let blob;

      if (!reader) {
        blob = await response.blob();
        setProgress(100);
      } else {
        const chunks = [];
        let received = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          received += value.length;
          setProgress(total ? (received / total) * 100 : Math.min(92, received / 4096));
        }
        blob = new Blob(chunks, { type: "application/octet-stream" });
      }

      saveBlob(blob, filename);
      setProgress(100);
      downloadBtn.classList.remove("loading");
      downloadBtn.classList.add("done");
      const label = $(".dl-label", downloadBtn);
      const badge = $(".dl-size", downloadBtn);
      if (label) label.textContent = t("dl.doneLabel");
      if (badge) badge.textContent = "✓";
      if (note) note.textContent = t("dl.doneNote");
      showToast(t("dl.toast", { size: sizeText(blob.size) }));

      window.setTimeout(() => {
        resetButton();
        if (note) note.innerHTML = noteHTML();
      }, 9000);
    } catch (error) {
      downloadBtn.classList.remove("loading");
      setProgress(0);
      if (note) note.textContent = t("dl.fallbackNote");
      window.location.href = DOWNLOAD_URL;
      showToast(t("dl.fallbackToast"));
    } finally {
      downloading = false;
    }
  };

  downloadBtn?.addEventListener("click", startDownload);

  finalBtn?.addEventListener("click", (event) => {
    event.preventDefault();
    startDownload();
    document.querySelector("#download")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  /* ---------- language ---------- */
  document.addEventListener("ksenus:lang", () => {
    resetButton();
    const map = titles();
    if (tourTitle && map[currentTab]) tourTitle.textContent = map[currentTab];
    if (pinStatus && !pin) pinStatus.textContent = PIN_HINT();
    if (note && !downloading && !downloadBtn?.classList.contains("done")) {
      note.innerHTML = noteHTML();
    }
  });

  resetButton();
})();
