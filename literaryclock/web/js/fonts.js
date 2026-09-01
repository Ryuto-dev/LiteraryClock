/**
 * 任意 Web フォントの実行時登録.
 *
 * web/fonts/ に woff2 が置かれている場合のみ @font-face を登録し、
 * 本文フォントスタックの先頭へ差し込む。
 *
 * こうしている理由:
 *  - CSS に url() を直書きすると、フォント未配置の環境で 404 が並ぶ
 *    (kiosk の DevTools コンソールが汚れ、起動も僅かに遅くなる)
 *  - どのファイルが存在するかは /api/fonts でサーバに聞くため、
 *    クライアント側は無駄なリクエストを一切出さない
 *  - フォントを同梱しない代わりに、無い場合は OS の日本語明朝で動作する
 *
 * フォントの取得: scripts/fetch_fonts.sh
 */

/** ファイル名 → 登録情報の対応表. */
const FONT_SPECS = {
  'ZenOldMincho-Regular.woff2':   { family: 'Zen Old Mincho', weight: '400',     role: 'serif' },
  'ZenOldMincho-Bold.woff2':      { family: 'Zen Old Mincho', weight: '600 700', role: 'serif' },
  'NotoSerifJP-Regular.woff2':    { family: 'Noto Serif JP',  weight: '400',     role: 'serif' },
  'NotoSerifJP-SemiBold.woff2':   { family: 'Noto Serif JP',  weight: '600 700', role: 'serif' },
  'JetBrainsMono-Regular.woff2':  { family: 'JetBrains Mono', weight: '400',     role: 'mono'  },
};

/** スタック先頭に置く優先順 (先にあるものを優先). */
const SERIF_PRIORITY = ['Zen Old Mincho', 'Noto Serif JP'];

/**
 * 配置済みの woff2 を FontFace API で登録する.
 * @returns {Promise<{serif: string[], mono: string[]}>} 適用できた family 名
 */
export async function loadOptionalFonts() {
  const loaded = { serif: [], mono: [] };

  if (!('FontFace' in window) || !document.fonts) {
    return loaded;
  }

  // どのフォントが存在するかサーバに問い合わせる
  let files = [];
  try {
    const res = await fetch('/api/fonts', { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      files = Array.isArray(data.available) ? data.available : [];
    }
  } catch {
    return loaded; // 問い合わせ失敗時は OS フォントで続行
  }

  if (files.length === 0) {
    return loaded;
  }

  await Promise.all(
    files.map(async (file) => {
      const spec = FONT_SPECS[file];
      if (!spec) return;
      try {
        const face = new FontFace(
          spec.family,
          `url("fonts/${file}") format("woff2")`,
          { weight: spec.weight, style: 'normal', display: 'swap' },
        );
        await face.load();
        document.fonts.add(face);
        if (!loaded[spec.role].includes(spec.family)) {
          loaded[spec.role].push(spec.family);
        }
      } catch (err) {
        console.warn(`[literaryclock] フォント読込失敗: ${file}`, err.message);
      }
    }),
  );

  // 読み込めた書体をスタック先頭へ差し込む
  const root = document.documentElement;
  const style = getComputedStyle(root);

  if (loaded.serif.length) {
    loaded.serif.sort(
      (a, b) => SERIF_PRIORITY.indexOf(a) - SERIF_PRIORITY.indexOf(b),
    );
    const current = style.getPropertyValue('--font-serif').trim();
    const prefix = loaded.serif.map((n) => `"${n}"`).join(', ');
    root.style.setProperty('--font-serif', `${prefix}, ${current}`);
  }
  if (loaded.mono.length) {
    const current = style.getPropertyValue('--font-mono').trim();
    const prefix = loaded.mono.map((n) => `"${n}"`).join(', ');
    root.style.setProperty('--font-mono', `${prefix}, ${current}`);
  }

  if (loaded.serif.length || loaded.mono.length) {
    console.info(
      '[literaryclock] Web フォントを適用:',
      [...loaded.serif, ...loaded.mono].join(', '),
    );
  }

  return loaded;
}
