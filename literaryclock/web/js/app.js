/**
 * 文学時計 — アプリ本体.
 *
 * 責務:
 *  1. 起動時に /api/bootstrap で設定 + 初回エントリを取得
 *  2. 10 分スロットの境界ぴったりに引用を差し替える
 *  3. 同一スロットに複数候補がある場合のローテーション
 *  4. HUD (デジタル時刻・進捗) の更新
 *  5. キーボード操作と設定の永続化
 *  6. 通信断・タブ復帰・時刻ジャンプへの耐性
 *
 * kiosk で 24 時間動かし続ける前提のため、
 * setInterval の累積誤差を避けて「次の境界までの残り時間」を毎回計算する。
 */

import { fetchBootstrap, fetchNow } from './api.js';
import { renderEntry, renderFirst, fitToScreen } from './render.js';
import { loadOptionalFonts } from './fonts.js';

const SLOT_MS = 10 * 60 * 1000;
const STORAGE_KEY = 'literaryclock.prefs.v1';

const THEMES = ['ink', 'washi', 'night', 'sepia', 'mono'];
const TRANSITIONS = ['fade', 'typewriter', 'blur', 'slide', 'none'];

const dom = {
  body: document.body,
  boot: document.getElementById('boot'),
  bootText: document.querySelector('.boot__text'),
  toast: document.getElementById('toast'),
  help: document.getElementById('help'),
  digitalTime: document.getElementById('digitalTime'),
  digitalSec: document.getElementById('digitalSec'),
  progressFill: document.getElementById('progressFill'),
};

/** 実行時状態 */
const state = {
  settings: {
    theme: 'ink',
    writing_mode: 'horizontal',
    transition: 'fade',
    font_scale: 1.0,
    highlight_excerpt: true,
    show_credit: true,
    show_digital_clock: true,
    show_progress: true,
    rotate_seconds: 0,
    fake_time: '',
    time_speed: 1.0,
  },
  entry: null,
  currentSlot: -1,
  rotation: 0,
  candidates: 1,
  failures: 0,
  rotateTimer: null,
  slotTimer: null,
  busy: false,
};

/* ==========================================================================
   設定の適用と永続化
   ========================================================================== */

/** localStorage に保存されたユーザ設定を読む (キー操作による変更分). */
function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function savePrefs() {
  try {
    const { theme, writing_mode, transition, font_scale,
            highlight_excerpt, show_credit, show_digital_clock,
            show_progress } = state.settings;
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        theme, writing_mode, transition, font_scale,
        highlight_excerpt, show_credit, show_digital_clock, show_progress,
      }),
    );
  } catch {
    /* プライベートモード等で保存できなくても動作に影響させない */
  }
}

/** 設定を DOM 属性へ反映する (CSS 側が属性セレクタで拾う). */
function applySettings() {
  const s = state.settings;
  const body = dom.body;

  body.dataset.theme = THEMES.includes(s.theme) ? s.theme : 'ink';
  body.dataset.transition = TRANSITIONS.includes(s.transition) ? s.transition : 'fade';

  // writing_mode = auto の場合、画面の向きで決める (縦長なら縦書き)
  let writing = s.writing_mode;
  if (writing === 'auto') {
    writing = window.innerHeight > window.innerWidth * 1.05 ? 'vertical' : 'horizontal';
  }
  body.dataset.writing = writing;

  body.dataset.highlight = s.highlight_excerpt ? 'on' : 'off';
  body.dataset.credit = s.show_credit ? 'on' : 'off';
  body.dataset.digital = s.show_digital_clock ? 'on' : 'off';
  body.dataset.progress = s.show_progress ? 'on' : 'off';

  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.setAttribute(
      'content',
      getComputedStyle(body).getPropertyValue('--bg').trim() || '#12100e',
    );
  }
}

/* ==========================================================================
   時刻の計算
   ========================================================================== */

/** 設定を考慮した「いまの時刻」を返す (fake_time / time_speed 対応). */
function currentTime() {
  const s = state.settings;
  if (s.fake_time) {
    const [hh, mm] = s.fake_time.split(':').map(Number);
    const d = new Date();
    d.setHours(hh, mm, 0, 0);
    return d;
  }
  if (s.time_speed && s.time_speed !== 1.0) {
    const now = new Date();
    const elapsed =
      now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
    const scaled = (elapsed * s.time_speed) % 86400;
    const d = new Date(now);
    d.setHours(0, 0, 0, 0);
    return new Date(d.getTime() + scaled * 1000);
  }
  return new Date();
}

function slotOf(date) {
  return Math.floor((date.getHours() * 60 + date.getMinutes()) / 10);
}

/** 次のスロット境界までのミリ秒. */
function msToNextSlot(date) {
  const s = state.settings;
  const speed = s.fake_time ? 0 : (s.time_speed || 1);
  if (speed === 0) return Number.POSITIVE_INFINITY; // 時刻固定時は待たない

  const msIntoSlot =
    (date.getMinutes() % 10) * 60000 +
    date.getSeconds() * 1000 +
    date.getMilliseconds();
  const remaining = SLOT_MS - msIntoSlot;
  return Math.max(250, remaining / speed);
}

/* ==========================================================================
   HUD
   ========================================================================== */
function updateHud() {
  const now = currentTime();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');

  dom.digitalTime.textContent = `${hh}:${mm}`;
  dom.digitalSec.textContent = ss;

  if (state.settings.show_progress) {
    const msIntoSlot =
      (now.getMinutes() % 10) * 60000 + now.getSeconds() * 1000;
    const pct = Math.min(100, (msIntoSlot / SLOT_MS) * 100);
    dom.progressFill.style.width = `${pct.toFixed(2)}%`;
  }
}

/* ==========================================================================
   トースト
   ========================================================================== */
let toastTimer = null;
function toast(message, ms = 2000) {
  dom.toast.textContent = message;
  dom.toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    dom.toast.classList.remove('is-visible');
  }, ms);
}

/* ==========================================================================
   引用の更新
   ========================================================================== */

/**
 * サーバから現在のエントリを取得して描画する.
 * @param {object} opts { force, animate, rotation }
 */
async function update({ force = false, animate = true, rotation = null } = {}) {
  if (state.busy) return;
  state.busy = true;

  try {
    const rot = rotation === null ? state.rotation : rotation;
    const entry = await fetchNow(rot);
    state.failures = 0;

    const changed = force || entry.slot !== state.currentSlot ||
                    entry.quote !== state.entry?.quote;

    if (!changed) return;

    state.entry = entry;
    state.currentSlot = entry.slot;
    state.candidates = entry.candidates || 1;

    await renderEntry(entry, {
      animate,
      transition: state.settings.transition,
      scale: state.settings.font_scale,
      duration: 900,
    });
    fitToScreen();
    scheduleRotation();
  } catch (err) {
    state.failures += 1;
    console.warn('[literaryclock] 更新に失敗:', err.message);
    // 連続失敗時のみ通知する (一時的なブレでは画面を汚さない)
    if (state.failures === 3) {
      toast('サーバに接続できません。再試行しています…', 4000);
    }
  } finally {
    state.busy = false;
  }
}

/** 次のスロット境界に合わせて更新を予約する (誤差を毎回リセット). */
function scheduleSlotUpdate() {
  clearTimeout(state.slotTimer);
  const delay = msToNextSlot(currentTime());
  if (!Number.isFinite(delay)) return;

  state.slotTimer = setTimeout(async () => {
    state.rotation = 0;
    await update({ animate: true });
    scheduleSlotUpdate();
  }, delay + 120); // 境界を確実に越えてから取得する
}

/** 同一スロット内で候補をローテーションする. */
function scheduleRotation() {
  clearInterval(state.rotateTimer);
  const sec = state.settings.rotate_seconds;
  if (!sec || sec <= 0 || state.candidates <= 1) return;

  state.rotateTimer = setInterval(() => {
    state.rotation += 1;
    update({ force: true, animate: true, rotation: state.rotation });
  }, sec * 1000);
}

/* ==========================================================================
   キーボード操作
   ========================================================================== */
function cycle(list, current, dir = 1) {
  const i = list.indexOf(current);
  return list[(i + dir + list.length) % list.length];
}

function onKeyDown(ev) {
  const key = ev.key.toLowerCase();

  if (!dom.help.hidden && (key === 'escape' || key === 'h')) {
    dom.help.hidden = true;
    return;
  }

  switch (key) {
    case 't': {
      state.settings.theme = cycle(THEMES, state.settings.theme);
      applySettings();
      savePrefs();
      toast(`テーマ: ${state.settings.theme}`);
      break;
    }
    case 'w': {
      const modes = ['horizontal', 'vertical', 'auto'];
      state.settings.writing_mode = cycle(modes, state.settings.writing_mode);
      applySettings();
      savePrefs();
      fitToScreen();
      toast(
        state.settings.writing_mode === 'vertical' ? '縦書き'
        : state.settings.writing_mode === 'horizontal' ? '横書き' : '自動',
      );
      break;
    }
    case 'a': {
      state.settings.transition = cycle(TRANSITIONS, state.settings.transition);
      applySettings();
      savePrefs();
      toast(`アニメーション: ${state.settings.transition}`);
      break;
    }
    case '+':
    case '=': {
      state.settings.font_scale = Math.min(3, +(state.settings.font_scale + 0.05).toFixed(2));
      savePrefs();
      update({ force: true, animate: false });
      toast(`文字サイズ: ${Math.round(state.settings.font_scale * 100)}%`);
      break;
    }
    case '-':
    case '_': {
      state.settings.font_scale = Math.max(0.4, +(state.settings.font_scale - 0.05).toFixed(2));
      savePrefs();
      update({ force: true, animate: false });
      toast(`文字サイズ: ${Math.round(state.settings.font_scale * 100)}%`);
      break;
    }
    case 'c': {
      state.settings.show_credit = !state.settings.show_credit;
      applySettings();
      savePrefs();
      break;
    }
    case 'd': {
      state.settings.show_digital_clock = !state.settings.show_digital_clock;
      applySettings();
      savePrefs();
      break;
    }
    case 'n': {
      // デバッグ: 次の候補があればそれ、無ければ次のスロットへ
      if (state.candidates > 1) {
        state.rotation += 1;
        update({ force: true, rotation: state.rotation });
      } else {
        const next = (state.currentSlot + 1) % 144;
        const hh = String(Math.floor((next * 10) / 60)).padStart(2, '0');
        const mm = String((next * 10) % 60).padStart(2, '0');
        state.settings.fake_time = `${hh}:${mm}`;
        clearTimeout(state.slotTimer);
        update({ force: true });
        toast(`プレビュー: ${hh}:${mm}（R で戻る）`, 2500);
      }
      break;
    }
    case 'r': {
      window.location.reload();
      break;
    }
    case 'f': {
      toggleFullscreen();
      break;
    }
    case 'h':
    case '?': {
      dom.help.hidden = !dom.help.hidden;
      break;
    }
    case 'm': {
      // 開発用: カーソルの表示切替
      dom.body.classList.toggle('show-cursor');
      break;
    }
    default:
      break;
  }
}

function toggleFullscreen() {
  const el = document.documentElement;
  if (!document.fullscreenElement) {
    el.requestFullscreen?.().catch(() => toast('全画面表示に切り替えられません'));
  } else {
    document.exitFullscreen?.();
  }
}

/* ==========================================================================
   起動
   ========================================================================== */
async function boot() {
  applySettings();

  let data = null;
  // サーバ起動直後はまだ応答しないことがあるため数回リトライする
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      data = await fetchBootstrap();
      break;
    } catch (err) {
      if (attempt === 4) {
        dom.body.dataset.state = 'error';
        dom.bootText.textContent = 'サーバに接続できません';
        console.error('[literaryclock] 起動失敗:', err);
        toast('サーバに接続できません。literary-clock を起動してください。', 10000);
        return;
      }
      await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
    }
  }

  // サーバ設定を土台に、端末側のユーザ設定を上書き適用する
  state.settings = { ...state.settings, ...data.settings, ...loadPrefs() };
  // fake_time / time_speed はサーバ側 (起動オプション) を常に優先する
  state.settings.fake_time = data.settings.fake_time || '';
  state.settings.time_speed = data.settings.time_speed || 1.0;
  state.settings.rotate_seconds = data.settings.rotate_seconds || 0;
  applySettings();

  const ds = data.dataset || {};
  console.info(
    `[literaryclock] v${data.version} / エントリ ${ds.entries} 件 / ` +
    `スロット ${ds.slots_filled}/${ds.slots_total}`,
  );

  const entry = data.entry;
  state.entry = entry;
  state.currentSlot = entry.slot;
  state.candidates = entry.candidates || 1;

  // 任意 Web フォント (web/fonts/ に置かれていれば) を適用してから描画する。
  // フォント確定前に描画するとレイアウトがちらつくため、
  // 上限 2.5 秒だけ待って、間に合わなければ OS フォントで先に表示する。
  try {
    await Promise.race([
      loadOptionalFonts().then(() => document.fonts.ready),
      new Promise((r) => setTimeout(r, 2500)),
    ]);
  } catch { /* フォント API が無くても続行 */ }

  renderFirst(entry, {
    transition: state.settings.transition,
    scale: state.settings.font_scale,
  });
  fitToScreen();

  dom.body.dataset.state = 'ready';
  updateHud();

  // データセットに欠損がある場合だけ、起動時に一度知らせる
  if (ds.missing && ds.missing.length > 0) {
    setTimeout(() => {
      toast(
        `${ds.missing.length} スロットが未収録です（直近の時刻で代替表示）`,
        5000,
      );
    }, 2500);
  }

  scheduleSlotUpdate();
  scheduleRotation();
  setInterval(updateHud, 1000);

  window.addEventListener('keydown', onKeyDown);

  // 画面回転・ウィンドウサイズ変更に追従
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      applySettings();
      fitToScreen();
    }, 220);
  });

  // スリープ復帰・タブ復帰時は時刻がずれている可能性があるため再同期
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      update({ animate: false });
      scheduleSlotUpdate();
      updateHud();
    }
  });

  // 保険: 30 秒ごとに時刻の整合性を確認する
  // (システム時刻の変更/NTP 同期/長時間スリープでタイマがずれた場合の復旧)
  setInterval(() => {
    const expected = slotOf(currentTime());
    if (expected !== state.currentSlot && !state.settings.fake_time) {
      update({ animate: true });
      scheduleSlotUpdate();
    }
  }, 30000);
}

boot();
