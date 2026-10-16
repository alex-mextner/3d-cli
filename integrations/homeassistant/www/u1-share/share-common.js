// Shared validation. The HA server, not this JavaScript, authorizes every frame.
export const MAX_TTL = 7 * 86400;
export const CAMERA = /^camera\.[a-z0-9_]+$/;

export function publicOrigin(value) {
  if (typeof value !== "string" || /[\s\\]/.test(value)) throw Error("Укажите внешний HTTPS-адрес HA.");
  let u;
  try { u = new URL(value); } catch { throw Error("Укажите внешний HTTPS-адрес HA."); }
  if (u.protocol !== "https:" || u.username || u.password || u.search || u.hash || !["", "/"].includes(u.pathname))
    throw Error("Нужен HTTPS-адрес без пути, параметров и логина.");
  const h = u.hostname.toLowerCase();
  // A DNS name makes accidental sharing of private/Tailscale IPs impossible.
  if (!h.includes(".") || /^[0-9.]+$/.test(h) || h.startsWith("[") || h.endsWith(".local") || h.endsWith(".localhost"))
    throw Error("Для публичной ссылки нужен внешний DNS-адрес, не локальный IP.");
  return u.origin;
}

export function validateSignedPath(path, entity = null) {
  if (typeof path !== "string" || path.length > 10000 || /[\s\\]/.test(path)) throw Error("Некорректная подпись HA.");
  const u = new URL(path, "https://validation.invalid");
  if (!path.startsWith("/api/camera_proxy/camera.") || u.origin !== "https://validation.invalid" || u.hash ||
      !/^\/api\/camera_proxy\/camera\.[a-z0-9_]+$/.test(u.pathname) ||
      (entity && u.pathname !== `/api/camera_proxy/${entity}`)) throw Error("Ссылка должна вести только на выбранную камеру.");
  const entries = [...u.searchParams.entries()];
  if (entries.length !== 1 || entries[0][0] !== "authSig" || !entries[0][1]) throw Error("Нет подписи authSig.");
  return path;
}

export function expiryFromPath(path, fallback) {
  try {
    const token = new URL(path, "https://validation.invalid").searchParams.get("authSig");
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(part + "=".repeat((4 - part.length % 4) % 4)));
    if (Number.isSafeInteger(claims.exp) && claims.exp > 0) return claims.exp;
  } catch { /* Display-only fallback; the server still validates the signature. */ }
  return fallback;
}

export function buildLinks(base, path, exp, refresh = 5) {
  base = publicOrigin(base);
  validateSignedPath(path);
  if (!Number.isSafeInteger(exp) || exp < 1 || !Number.isInteger(refresh) || refresh < 2 || refresh > 60)
    throw Error("Некорректный срок или интервал.");
  const data = { v: 1, path, exp, refresh };
  return { image: base + path, viewer: `${base}/local/u1-share/viewer.html#${encodeURIComponent(JSON.stringify(data))}` };
}

export function parseShare(fragment) {
  if (fragment.length > 16000) throw Error("Слишком длинная ссылка.");
  let data;
  try { data = JSON.parse(decodeURIComponent(fragment.replace(/^#/, ""))); }
  catch { throw Error("Ссылка отсутствует или повреждена."); }
  if (!data || data.v !== 1 || !Number.isSafeInteger(data.exp) || data.exp < 1 ||
      !Number.isInteger(data.refresh) || data.refresh < 2 || data.refresh > 60) throw Error("Некорректная ссылка.");
  validateSignedPath(data.path);
  // A modified expiry in the fragment can never extend the server's signature.
  return { ...data, exp: Math.min(data.exp, expiryFromPath(data.path, data.exp)) };
}
