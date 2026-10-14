import { parseShare } from "./share-common.js";
const el = id => document.getElementById(id);
let share, stopped = false, paused = false, inflight = false, timer, clock, aborter, objectURL;
const clearFrame = () => {
  el("frame").hidden = true;
  el("frame").removeAttribute("src");
  if (objectURL) URL.revokeObjectURL(objectURL);
  objectURL = null;
};
function stop(message, badge = "Доступ завершён") {
  stopped = true; clearTimeout(timer); clearInterval(clock); aborter?.abort(); clearFrame();
  el("empty").hidden = false; el("empty").textContent = message;
  el("status").textContent = message; el("badge").textContent = badge; el("pause").disabled = true;
}
function expired() { return Date.now() >= share.exp * 1000; }
function tick() { if (!stopped && expired()) stop("Срок действия ссылки истёк. Попросите новую ссылку у владельца."); }
async function frame() {
  if (stopped || inflight) return;
  if (expired()) { tick(); return; }
  if (paused || document.hidden) return;
  inflight = true; aborter = new AbortController();
  const requestTimeout = setTimeout(() => aborter.abort(), 15000);
  try {
    // Never append a timestamp to the signed path: newer HA signs query parameters.
    // No ambient login may mask an expired signature; every fetch is anonymous.
    const response = await fetch(share.path, { cache: "no-store", credentials: "omit", redirect: "error",
      referrerPolicy: "no-referrer", signal: aborter.signal });
    if ([401, 403, 404].includes(response.status)) {
      stop("Ссылка истекла, была отозвана или камера больше недоступна."); return;
    }
    if (!response.ok) throw Error("unavailable");
    const mime = response.headers.get("content-type")?.split(";")[0].trim();
    if (!["image/jpeg", "image/png", "image/webp"].includes(mime)) throw Error("not-image");
    const blob = await response.blob();
    if (!blob.size || blob.size > 15 * 1024 * 1024) throw Error("image-size");
    if (stopped || paused || document.hidden || expired()) { tick(); return; }
    const nextURL = URL.createObjectURL(blob);
    const decoded = new Image(); decoded.src = nextURL;
    try { await decoded.decode(); }
    catch { URL.revokeObjectURL(nextURL); throw Error("decode"); }
    if (stopped || paused || document.hidden || expired()) { URL.revokeObjectURL(nextURL); tick(); return; }
    const previous = objectURL;
    objectURL = nextURL; el("frame").src = objectURL;
    el("frame").hidden = false; el("empty").hidden = true;
    if (previous) URL.revokeObjectURL(previous);
    el("badge").textContent = "Обновляется";
    el("status").textContent = `Последний кадр: ${new Date().toLocaleTimeString()} · интервал ${share.refresh} сек.`;
  } catch {
    if (!stopped && !paused && !document.hidden) {
      // Don't leave a frozen image looking like a live view.
      clearFrame(); el("empty").hidden = false;
      el("empty").textContent = "Камера временно недоступна. Повторяю подключение…";
      el("status").textContent = "Нет свежего кадра"; el("badge").textContent = "Нет сигнала";
    }
  } finally {
    clearTimeout(requestTimeout); inflight = false;
    if (!stopped && !paused && !document.hidden) timer = setTimeout(frame, share.refresh * 1000);
  }
}
el("pause").addEventListener("click", () => {
  if (stopped) return;
  paused = !paused; clearTimeout(timer); aborter?.abort();
  el("pause").textContent = paused ? "Продолжить" : "Пауза";
  el("badge").textContent = paused ? "Пауза" : "Подключение";
  if (paused) el("status").textContent = "Пауза: изображение не обновляется";
  else { timer = setTimeout(frame, 0); }
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (document.hidden) aborter?.abort();
  else { tick(); if (!paused && !stopped) timer = setTimeout(frame, 0); }
});
window.addEventListener("pagehide", () => { clearTimeout(timer); clearInterval(clock); aborter?.abort(); clearFrame(); });
window.addEventListener("pageshow", event => {
  if (event.persisted && share && !stopped) { clock = setInterval(tick, 250); tick(); frame(); }
});
try {
  share = parseShare(location.hash);
  el("expires").textContent = `Доступ до ${new Date(share.exp * 1000).toLocaleString()}`;
  if (expired()) tick();
  else { clock = setInterval(tick, 250); frame(); }
} catch (error) { stop(error.message || "Некорректная ссылка.", "Ошибка ссылки"); }
