function majorVersion(userAgent, pattern) {
  const match = String(userAgent || "").match(pattern);
  return match?.[1] ? ` ${match[1]}` : "";
}

export function describeSessionUserAgent(userAgent = "") {
  const ua = String(userAgent || "");
  let os = "未知设备";
  let deviceKind = "desktop";

  if (/iPhone/i.test(ua)) {
    os = "iPhone";
    deviceKind = "mobile";
  } else if (/iPad/i.test(ua)) {
    os = "iPad";
    deviceKind = "mobile";
  } else if (/Android/i.test(ua)) {
    os = "Android";
    deviceKind = "mobile";
  } else if (/Windows NT/i.test(ua)) {
    os = "Windows";
  } else if (/Macintosh|Mac OS X/i.test(ua)) {
    os = "macOS";
  } else if (/Linux/i.test(ua)) {
    os = "Linux";
  }

  let browser = "未知浏览器";
  if (/Edg\//i.test(ua)) browser = `Edge${majorVersion(ua, /Edg\/(\d+)/i)}`;
  else if (/OPR\//i.test(ua)) browser = `Opera${majorVersion(ua, /OPR\/(\d+)/i)}`;
  else if (/CriOS\//i.test(ua)) browser = `Chrome${majorVersion(ua, /CriOS\/(\d+)/i)}`;
  else if (/Chrome\//i.test(ua)) browser = `Chrome${majorVersion(ua, /Chrome\/(\d+)/i)}`;
  else if (/FxiOS\//i.test(ua)) browser = `Firefox${majorVersion(ua, /FxiOS\/(\d+)/i)}`;
  else if (/Firefox\//i.test(ua)) browser = `Firefox${majorVersion(ua, /Firefox\/(\d+)/i)}`;
  else if (/Safari\//i.test(ua)) browser = `Safari${majorVersion(ua, /Version\/(\d+)/i)}`;

  return { os, browser, deviceKind, label: `${os} · ${browser}` };
}

export function formatSessionTime(timestamp) {
  const value = Number(timestamp || 0);
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}

export function relativeSessionTime(timestamp, nowMs = Date.now()) {
  const value = Number(timestamp || 0);
  if (!value) return "未知";
  const seconds = Math.max(0, Math.floor((Number(nowMs) - value * 1000) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return formatSessionTime(value);
}
