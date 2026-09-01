/**
 * サーバ API クライアント.
 *
 * kiosk 運用では「落ちない」ことが最優先なので、
 * 失敗時は例外を投げるだけに留め、再試行の判断は呼び出し側に任せる。
 */

const TIMEOUT_MS = 8000;

async function request(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(path, {
      signal: controller.signal,
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} ${res.statusText}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 起動時の設定とデータセット情報をまとめて取得する. */
export function fetchBootstrap() {
  return request('/api/bootstrap');
}

/** 現在時刻に対応する引用を取得する. */
export function fetchNow(rotation = 0) {
  return request(`/api/now?rotation=${encodeURIComponent(rotation)}`);
}

/** 指定時刻 (HH:MM) に対応する引用を取得する. */
export function fetchAt(time, rotation = 0) {
  return request(
    `/api/at?time=${encodeURIComponent(time)}&rotation=${encodeURIComponent(rotation)}`,
  );
}
