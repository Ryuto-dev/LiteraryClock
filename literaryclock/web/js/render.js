/**
 * 引用の描画とアニメーション.
 *
 * 日本語組版で気をつけている点:
 *  - excerpt (時刻部分) を before / after と分けて span 化し、強調できるようにする
 *  - 文字数に応じてフォントサイズを自動調整し、どの引用でも画面に収める
 *  - typewriter モードでは書記素単位で分割する (濁点・異体字セレクタを壊さない)
 */

const els = {
  frame: document.querySelector('.quote-frame'),
  quote: document.getElementById('quote'),
  before: document.getElementById('quoteBefore'),
  excerpt: document.getElementById('quoteExcerpt'),
  after: document.getElementById('quoteAfter'),
  author: document.getElementById('creditAuthor'),
  title: document.getElementById('creditTitle'),
};

/* --------------------------------------------------------------------------
   文字サイズの自動調整

   引用文は 10 文字程度から 300 文字近くまで幅がある。
   固定サイズだと短文は寂しく、長文は溢れるため、文字数から基準サイズを決める。
   -------------------------------------------------------------------------- */
const SIZE_STEPS = [
  { max: 24,  size: 7.4 },
  { max: 40,  size: 6.4 },
  { max: 60,  size: 5.6 },
  { max: 85,  size: 4.9 },
  { max: 115, size: 4.3 },
  { max: 150, size: 3.8 },
  { max: 200, size: 3.3 },
  { max: 260, size: 2.9 },
  { max: Infinity, size: 2.5 },
];

function baseSizeFor(length) {
  for (const step of SIZE_STEPS) {
    if (length <= step.max) return step.size;
  }
  return 2.5;
}

/** 書記素 (結合文字を含む 1 文字) 単位に分割する. */
function splitGraphemes(text) {
  if (typeof Intl !== 'undefined' && Intl.Segmenter) {
    const seg = new Intl.Segmenter('ja', { granularity: 'grapheme' });
    return Array.from(seg.segment(text), (s) => s.segment);
  }
  return Array.from(text);
}

/**
 * 縦書き用に 1〜2 桁の半角数字を <span class="tcy"> で包む (縦中横).
 *
 * 青空文庫のテキストには「四十二」のような漢数字だけでなく
 * 「42」のような半角数字も現れる。縦書きでそのまま流すと
 * 数字が 1 文字ずつ縦に並んでしまうため、2 桁までは横に組む。
 * 3 桁以上は縦中横にすると窮屈なので、そのまま横倒しにする。
 */
function appendWithTcy(parent, text) {
  const re = /\d{1,2}(?!\d)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    const span = document.createElement('span');
    span.className = 'tcy';
    span.textContent = m[0];
    parent.appendChild(span);
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parent.appendChild(document.createTextNode(text.slice(last)));
  }
}

/**
 * 引用文を DOM へ流し込む.
 * typewriter モードの場合は 1 文字ずつ span に包み、遅延を付ける。
 */
function paint(entry, { typewriter, scale }) {
  const size = (baseSizeFor(entry.length) * scale).toFixed(2);
  els.quote.style.setProperty('--quote-size', `${size}vmin`);

  const parts = [
    [els.before, entry.before],
    [els.excerpt, entry.excerpt],
    [els.after, entry.after],
  ];

  // before/after が空で quote だけある場合の保険
  if (!entry.before && !entry.after && entry.quote && !entry.excerpt) {
    parts[0][1] = entry.quote;
  }

  if (!typewriter) {
    for (const [el, text] of parts) {
      el.textContent = '';
      if (text) appendWithTcy(el, text);
    }
  } else {
    // 全体で通し番号を振り、連続的に文字が現れるようにする
    let index = 0;
    const total = entry.length || 1;
    // 長文ほど 1 文字あたりの間隔を詰め、全体の所要時間を一定に保つ
    const perChar = Math.max(12, Math.min(46, 1600 / total));

    for (const [el, text] of parts) {
      el.textContent = '';
      if (!text) continue;
      const frag = document.createDocumentFragment();
      for (const ch of splitGraphemes(text)) {
        if (ch === ' ' || ch === '\u3000') {
          frag.appendChild(document.createTextNode(ch));
          index += 1;
          continue;
        }
        const span = document.createElement('span');
        // 半角数字は縦書き時に縦中横で組む
        span.className = /^\d$/.test(ch) ? 'ch tcy' : 'ch';
        span.textContent = ch;
        span.style.setProperty('--d', `${Math.round(index * perChar)}ms`);
        frag.appendChild(span);
        index += 1;
      }
      el.appendChild(frag);
    }
  }

  els.author.textContent = entry.author || '';
  els.title.textContent = entry.title || '';

  // スクリーンリーダー向けに全文を提供する
  els.quote.setAttribute(
    'aria-label',
    `${entry.quote}（${entry.author}『${entry.title}』）`,
  );
}

/** excerpt の下線アニメーションを再生し直す. */
function restartAccents() {
  const el = els.excerpt;
  el.style.animation = 'none';
  const credit = document.getElementById('credit');
  credit.style.animation = 'none';
  // 強制的にリフローさせてアニメーションを巻き戻す
  void el.offsetWidth;
  el.style.animation = '';
  credit.style.animation = '';
}

/**
 * 引用を差し替える.
 *
 * @param {object} entry  API から取得したエントリ
 * @param {object} opts   { animate, transition, scale, duration }
 */
export async function renderEntry(entry, opts = {}) {
  const {
    animate = true,
    transition = 'fade',
    scale = 1,
    duration = 900,
  } = opts;

  const typewriter = transition === 'typewriter';

  if (!animate || transition === 'none') {
    paint(entry, { typewriter: false, scale });
    restartAccents();
    return;
  }

  els.frame.style.setProperty('--anim-dur', `${duration}ms`);

  // 退場
  els.frame.classList.add('is-out');
  await wait(duration * 0.62);

  // 差し替え (退場が終わってから)
  paint(entry, { typewriter, scale });

  // 入場: is-out → is-in へ切り替え、次フレームで解除して遷移させる
  els.frame.classList.remove('is-out');
  els.frame.classList.add('is-in');
  restartAccents();

  await nextFrame();
  await nextFrame();
  els.frame.classList.remove('is-in');
}

/** 初回描画 (退場アニメーションなし). */
export function renderFirst(entry, opts = {}) {
  const { transition = 'fade', scale = 1 } = opts;
  paint(entry, { typewriter: transition === 'typewriter', scale });
  restartAccents();
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nextFrame() {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

/**
 * 引用がフレームから溢れていないか検査し、必要なら縮小する.
 * フォントの実測が必要なため、描画後に呼ぶ。
 */
export function fitToScreen(maxTries = 6) {
  const quote = els.quote;
  const stageH = window.innerHeight * 0.82;
  const stageW = window.innerWidth * 0.9;
  let tries = 0;

  while (tries < maxTries) {
    const rect = quote.getBoundingClientRect();
    const overflowY = rect.height > stageH;
    const overflowX = rect.width > stageW;
    if (!overflowY && !overflowX) break;

    const current = parseFloat(
      getComputedStyle(quote).getPropertyValue('--quote-size'),
    );
    if (!Number.isFinite(current) || current <= 1.6) break;
    quote.style.setProperty('--quote-size', `${(current * 0.92).toFixed(2)}vmin`);
    tries += 1;
  }
}
