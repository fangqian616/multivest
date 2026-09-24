/*!
 * md.js — 安全、零依赖的轻量 Markdown 渲染器
 *
 * 用途：把大语言模型生成的研判报告 / 分析师发言 / 风控意见（Markdown 文本）
 *      渲染成可直接放进卡片的 HTML 片段。
 *
 * 设计约束：
 *   1. 普通 script（非 ES module），加载后暴露 window.MD = { render, inline, plain }；
 *   2. 零依赖，不用 CDN、不引第三方库；
 *   3. 安全第一：所有用户文本先 HTML 转义，再做 Markdown 转换；模型输出视为不可信内容；
 *      链接只放行 http/https，其余（javascript:、data:、vbscript: …）降级为纯文本；
 *   4. 纯函数，无 DOM 依赖，任何输入都不抛异常；
 *   5. 只输出 class（md-h / md-table / md-pre / md-code / md-quote / md-hr / md-ul / md-ol / md-p），
 *      不写内联样式（表格对齐属性除外），样式由外部 CSS 提供。
 *
 * 三个入口：
 *   MD.render(text) -> HTML 字符串（块级 + 行内，含盘古之白）
 *   MD.inline(text) -> HTML 字符串（只处理行内语法，用于表格单元格 / 列表项 / 标题内部）
 *   MD.plain(text)  -> 纯文本（剥掉全部 Markdown 标记，用于 title / 摘要截断 / 搜索）
 *
 * 关于 plain() 的转义约定：它返回的是「未做 HTML 转义的纯文本」，
 * 这样既能直接用于搜索匹配、按字数截断（不会截出 &amp; 这类残片），
 * 也避免与调用方已有的 esc() 重复转义。若要把结果拼进 HTML，请调用方自行转义。
 */
(function () {
  'use strict';

  /* ================================================================== *
   * 0. 基础工具
   * ================================================================== */

  /** null / undefined / 数字 / 对象等一律视为空文本；顺带清掉占位符用的 NUL */
  function toText(value) {
    return typeof value === 'string' ? value.replace(/\u0000/g, '') : '';
  }

  /** HTML 转义：一切进入输出的文本都必须先过这一关 */
  function esc(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** 只放行 http / https，其余协议（javascript:、data:、vbscript:…）一律拒绝 */
  function isSafeUrl(url) {
    return /^https?:\/\//i.test(String(url).trim());
  }

  /** 行内占位符仓库：先把已生成的 HTML 藏起来，避免被后续规则二次加工 */
  function makeStore() {
    var items = [];
    return {
      put: function (html) {
        items.push(html);
        return '\u0000' + (items.length - 1) + '\u0000';
      },
      dump: function (text) {
        var out = text.replace(/\u0000(\d+)\u0000/g, function (m, i) {
          var v = items[Number(i)];
          return v === undefined ? '' : v;
        });
        return out.replace(/\u0000\d*\u0000/g, ''); // 兜底：不留任何占位符痕迹
      }
    };
  }

  /* ================================================================== *
   * 1. 盘古之白：中英文之间自动加半角空格
   *
   *    克制规则：只在「汉字 / 中文句读标点」与「ASCII 字母」直接相邻时插一个空格。
   *    - 不碰数字与符号：-20%、3.5%、2026-09-22、沪深300指数 原样保留；
   *    - 不碰成对全角括号与引号：（LLM）、「LLM」不会变成「（ LLM ）」；
   *    - 只作用于文本节点，不进入 HTML 标签、属性，也不进入代码块/行内代码。
   * ================================================================== */

  var PAN_IDEO = '\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff' + // 汉字
                 '\u3005\u3007';                              // 々 〇
  var PAN_PUNCT = '\u3001\u3002\uff01\uff0c\uff1a\uff1b\uff1f' + // 、。！，：；？
                  '\u2026\u2014';                               // … —
  var PAN_CLASS = PAN_IDEO + PAN_PUNCT;
  var RE_PAN_WIDE = new RegExp('[' + PAN_CLASS + ']');
  var RE_PAN_IDEO = new RegExp('[' + PAN_IDEO + ']');
  var RE_PAN_A = new RegExp('([' + PAN_CLASS + '])([A-Za-z])', 'g');
  var RE_PAN_B = new RegExp('([A-Za-z])([' + PAN_IDEO + '])', 'g');

  function pangu(text) {
    var prev = null;
    for (var i = 0; i < 8 && text !== prev; i++) {
      prev = text;
      text = text.replace(RE_PAN_A, '$1 $2').replace(RE_PAN_B, '$1 $2');
    }
    return text;
  }

  function isWide(ch) {
    return !!ch && RE_PAN_WIDE.test(ch);
  }

  /** 汉字（不含标点）：标点前面不加空格，所以「英文 + 标点」这一侧要单独排除 */
  function isIdeo(ch) {
    return !!ch && RE_PAN_IDEO.test(ch);
  }

  function isLatin(ch) {
    return !!ch && ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z'));
  }

  /* ================================================================== *
   * 2. 行内渲染
   * ================================================================== */

  /** 图片 alt 只保留文字：去掉强调 / 代码标记 */
  function stripInline(text) {
    return String(text)
      .replace(/`+/g, '')
      .replace(/\*\*|__|~~/g, '')
      .replace(/(^|[^*\w])\*([^*]+)\*/g, '$1$2')
      .replace(/(^|[^_\w])_([^_]+)_/g, '$1$2');
  }

  function linkHtml(url, labelHtml) {
    return '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + labelHtml + '</a>';
  }

  /**
   * 行内解析核心。
   * 顺序很关键：占位 → 转义 → 强调 → 还原。这样保证「先转义、后转换」，
   * 同时已生成的 HTML 不会被后续规则破坏。
   */
  function inlineCore(text, depth, noLink, store) {
    if (typeof text !== 'string') return '';
    store = store || makeStore();
    // 顶层才清洗 NUL；嵌套调用必须保留上层占位符，否则链接里的图片会丢
    var s = depth === 0 ? text.replace(/\u0000/g, '') : text;

    // (1) 反斜杠转义：\* \_ \` 等保留为字面量
    s = s.replace(/\\([\\`*_{}\[\]()#+\-.!~>|])/g, function (m, ch) {
      return store.put(esc(ch));
    });

    // (2) 行内代码（内容原样转义，不再解析）
    s = s.replace(/(`+)([\s\S]*?)\1/g, function (m, ticks, code) {
      var body = code;
      if (/^ .* $/.test(body) && body.trim() !== '') body = body.slice(1, -1);
      return store.put('<code class="md-code">' + esc(body) + '</code>');
    });

    // (3) 图片：看板不需要外链图片，也不该发起外链请求 → 降级为文字
    s = s.replace(/!\[([^\]]*)\]\(([^)]*)\)/g, function (m, alt) {
      return store.put('[图片: ' + esc(stripInline(alt)) + ']');
    });

    if (!noLink) {
      // (4) 链接：只允许 http/https，其余保持纯文本
      s = s.replace(/\[([^\]]*)\]\(([^)]*)\)/g, function (m, label, url) {
        var u = String(url).trim().replace(/^<|>$/g, '');
        if (!isSafeUrl(u)) return store.put(esc(m));
        return store.put(linkHtml(u, inlineCore(label, depth + 1, true, store)));
      });

      // (5) 裸露 URL 自动识别
      s = s.replace(/https?:\/\/[^\s<>"'`）】」，。；！？、]+/gi, function (m) {
        var url = m;
        var tail = '';
        while (url.length > 1) {
          var last = url.charAt(url.length - 1);
          if ('.,;:!?*_~'.indexOf(last) >= 0) { tail = last + tail; url = url.slice(0, -1); continue; }
          if (last === ')' && url.split('(').length <= url.split(')').length) {
            tail = last + tail; url = url.slice(0, -1); continue;
          }
          break;
        }
        if (!isSafeUrl(url)) return m;
        return store.put(linkHtml(url, esc(url))) + tail;
      });
    }

    // (6) 转义其余全部文本（含上面保留下来的原始 Markdown 片段）
    s = esc(s);

    // (7) 强调：先处理三重标记（粗+斜），再双，再单；未闭合的一律原样保留
    s = s.replace(/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/(?<![A-Za-z0-9_])___(?=\S)([\s\S]*?\S)___(?![A-Za-z0-9_])/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(?<![A-Za-z0-9_])__(?=\S)([\s\S]*?\S)__(?![A-Za-z0-9_])/g, '<strong>$1</strong>');
    s = s.replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
    s = s.replace(/(?<![A-Za-z0-9*])\*(?=\S)([^*]*?\S)\*(?![A-Za-z0-9*])/g, '<em>$1</em>');
    s = s.replace(/(?<![A-Za-z0-9_])_(?=\S)([^_]*?\S)_(?![A-Za-z0-9_])/g, '<em>$1</em>');

    // (8) 还原占位内容
    return store.dump(s);
  }

  /** 行内渲染：表格单元格、列表项内部、标题内部。注意：不追加盘古空格 */
  function inline(text) {
    return inlineCore(text, 0, false);
  }

  /* ================================================================== *
   * 3. 块级渲染
   * ================================================================== */

  var RE_BLANK = /^[ \t]*$/;
  var RE_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^\n`]*)$/;
  var RE_HR = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
  var RE_HEAD = /^ {0,3}(#{1,6})[ \t]+(.*)$/;
  var RE_HEAD_EMPTY = /^ {0,3}(#{1,6})[ \t]*$/;
  var RE_QUOTE = /^ {0,3}>[ \t]?(.*)$/;
  var RE_UL = /^([ \t]*)([-*+])[ \t]+(.*)$/;
  var RE_OL = /^([ \t]*)(\d{1,9})([.)])[ \t]+(.*)$/;

  function leadingWidth(line) {
    var k = 0;
    while (k < line.length && (line.charAt(k) === ' ' || line.charAt(k) === '\t')) k++;
    return k;
  }

  function stripIndent(line, n) {
    var k = 0;
    while (k < line.length && k < n && (line.charAt(k) === ' ' || line.charAt(k) === '\t')) k++;
    return line.slice(k);
  }

  /* ---- 表格 ---- */

  /** 拆一行表格；\| 视为单元格内的字面竖线，反引号代码段内的竖线也不拆列 */
  function splitRow(line) {
    var s = String(line).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|' && s.charAt(s.length - 2) !== '\\') s = s.slice(0, -1);
    var out = [];
    var cur = '';
    var ticks = 0; // 0 = 不在代码段内
    for (var i = 0; i < s.length; i++) {
      var ch = s.charAt(i);
      if (ch === '`') {
        var n = 0;
        while (s.charAt(i + n) === '`') n++;
        if (ticks === 0) ticks = n;
        else if (ticks === n) ticks = 0;
        cur += s.substr(i, n);
        i += n - 1;
        continue;
      }
      if (ticks === 0 && ch === '\\' && s.charAt(i + 1) === '|') { cur += '|'; i++; continue; }
      if (ticks === 0 && ch === '|') { out.push(cur.trim()); cur = ''; continue; }
      cur += ch;
    }
    out.push(cur.trim());
    return out;
  }

  /** GFM 分隔行：每个单元格都是 :--- / ---: / :---: / --- */
  function isDelimRow(line) {
    if (line == null || line.indexOf('-') < 0) return false;
    var cells = splitRow(line);
    if (!cells.length) return false;
    for (var i = 0; i < cells.length; i++) {
      if (!/^:?-+:?$/.test(cells[i])) return false;
    }
    return true;
  }

  /** 表格必须「表头行 + 分隔行」成对出现，缺分隔行就只是普通段落 */
  function isTableStart(lines, i) {
    if (i + 1 >= lines.length) return false;
    if (String(lines[i]).indexOf('|') < 0) return false;
    return isDelimRow(lines[i + 1]);
  }

  function alignOf(cell) {
    var c = String(cell).trim();
    var left = c.charAt(0) === ':';
    var right = c.charAt(c.length - 1) === ':';
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return '';
  }

  function styleOf(align) {
    return align ? ' style="text-align:' + align + '"' : '';
  }

  function parseTable(lines, start) {
    var head = splitRow(lines[start]);
    var aligns = splitRow(lines[start + 1]);
    var n = head.length > 0 ? head.length : 1;
    var align = [];
    var k;
    for (k = 0; k < n; k++) align.push(aligns[k] === undefined ? '' : alignOf(aligns[k]));

    var html = '<table class="md-table"><thead><tr>';
    for (k = 0; k < n; k++) html += '<th' + styleOf(align[k]) + '>' + inline(head[k] || '') + '</th>';
    html += '</tr></thead><tbody>';

    var i = start + 2;
    while (i < lines.length && !RE_BLANK.test(lines[i]) && String(lines[i]).indexOf('|') >= 0) {
      var cells = splitRow(lines[i]);
      html += '<tr>';
      for (k = 0; k < n; k++) {
        // 少列补空、多列截断，绝不越界
        html += '<td' + styleOf(align[k]) + '>' + inline(cells[k] === undefined ? '' : cells[k]) + '</td>';
      }
      html += '</tr>';
      i++;
    }
    html += '</tbody></table>';
    return { html: html, next: i };
  }

  /* ---- 代码块 ---- */

  function parseFence(lines, start) {
    var m = RE_FENCE.exec(lines[start]);
    var ch = m[1].charAt(0);
    var len = m[1].length;
    var closeRe = new RegExp('^ {0,3}' + (ch === '`' ? '`' : '~') + '{' + len + ',}[ \\t]*$');
    var buf = [];
    var i = start + 1;
    while (i < lines.length) {
      if (closeRe.test(lines[i])) { i++; break; } // 正常闭合
      buf.push(lines[i]);
      i++;                                        // 未闭合 → 一直延伸到文末
    }
    return {
      html: '<pre class="md-pre"><code>' + esc(buf.join('\n')) + '</code></pre>',
      next: i
    };
  }

  /* ---- 引用 ---- */

  function parseQuote(lines, start) {
    var buf = [];
    var i = start;
    while (i < lines.length) {
      var m = RE_QUOTE.exec(lines[i]);
      if (m) { buf.push(m[1]); i++; continue; }
      // 惰性续行：既不是空行、也不是新块起始 → 仍属于引用
      if (!RE_BLANK.test(lines[i]) && !isBlockStart(lines, i)) { buf.push(lines[i]); i++; continue; }
      break;
    }
    return { html: '<blockquote class="md-quote">' + parseBlocks(buf) + '</blockquote>', next: i };
  }

  /* ---- 列表 ---- */

  function listMatch(line) {
    var m = RE_UL.exec(line);
    if (m) return { indent: m[1].length, ordered: false, width: m[2].length + 1, text: m[3], num: 0 };
    m = RE_OL.exec(line);
    if (m) return { indent: m[1].length, ordered: true, width: m[2].length + m[3].length + 1, text: m[4], num: parseInt(m[2], 10) };
    return null;
  }

  /** 松散列表的 <li> 内保留 <p>；紧凑列表把首个段落外壳去掉 */
  function unwrapFirstParagraph(html) {
    var m = /^<p class="md-p">([\s\S]*?)<\/p>([\s\S]*)$/.exec(html);
    return m ? m[1] + m[2] : html;
  }

  function parseList(lines, start) {
    var first = listMatch(lines[start]);
    var base = first.indent;
    var ordered = first.ordered;
    var startNum = first.num;
    var items = [];
    var loose = false;
    var i = start;

    while (i < lines.length) {
      var m = listMatch(lines[i]);
      if (!m || m.ordered !== ordered || m.indent !== base) break;
      if (items.length === 0) startNum = m.num;

      var contentIndent = base + m.width;
      var buf = [m.text];
      i++;

      while (i < lines.length) {
        var line = lines[i];
        if (RE_BLANK.test(line)) {
          var j = i;
          while (j < lines.length && RE_BLANK.test(lines[j])) j++;
          if (j >= lines.length) { i = j; break; }
          var nm = listMatch(lines[j]);
          if (nm && nm.indent === base && nm.ordered === ordered) { loose = true; i = j; break; }
          if (leadingWidth(lines[j]) >= contentIndent) { loose = true; buf.push(''); i++; continue; }
          i = j;
          break;
        }
        if (leadingWidth(line) >= contentIndent) { buf.push(stripIndent(line, contentIndent)); i++; continue; }
        break; // 缩进不足：本项结束（交给外层处理）
      }
      items.push(buf);
    }

    var tag = ordered ? 'ol' : 'ul';
    var html = '';
    for (var k = 0; k < items.length; k++) {
      var body = parseBlocks(items[k]);
      if (!loose) body = unwrapFirstParagraph(body);
      html += '<li>' + body + '</li>';
    }
    var startAttr = ordered && startNum > 1 ? ' start="' + startNum + '"' : '';
    return {
      html: '<' + tag + ' class="md-' + tag + '"' + startAttr + '>' + html + '</' + tag + '>',
      next: i
    };
  }

  /* ---- 段落 ---- */

  function isBlockStart(lines, i) {
    var line = lines[i];
    if (line === undefined) return true;
    if (RE_BLANK.test(line)) return true;
    if (RE_FENCE.test(line)) return true;
    if (RE_HR.test(line)) return true;
    if (RE_HEAD.test(line) || RE_HEAD_EMPTY.test(line)) return true;
    if (RE_QUOTE.test(line)) return true;
    if (RE_UL.test(line) || RE_OL.test(line)) return true;
    if (isTableStart(lines, i)) return true;
    return false;
  }

  /** 块级主循环 */
  function parseBlocks(lines) {
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (RE_BLANK.test(line)) { i++; continue; }

      var fm = RE_FENCE.exec(line);
      if (fm) { var f = parseFence(lines, i); out.push(f.html); i = f.next; continue; }

      if (RE_HR.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }

      // 标题降级映射：# → h4、## → h5、### 及更深 → h6，避免模型的一级标题在卡片里过大
      var hm = RE_HEAD.exec(line) || RE_HEAD_EMPTY.exec(line);
      if (hm) {
        var lv = Math.min(6, hm[1].length + 3);
        var title = String(hm[2] === undefined ? '' : hm[2]).replace(/[ \t]+#+[ \t]*$/, '');
        out.push('<h' + lv + ' class="md-h">' + inline(title) + '</h' + lv + '>');
        i++;
        continue;
      }

      if (RE_QUOTE.test(line)) { var q = parseQuote(lines, i); out.push(q.html); i = q.next; continue; }

      if (RE_UL.test(line) || RE_OL.test(line)) { var li = parseList(lines, i); out.push(li.html); i = li.next; continue; }

      if (isTableStart(lines, i)) { var tb = parseTable(lines, i); out.push(tb.html); i = tb.next; continue; }

      // 段落：连续的非块起始行，段内单个换行 → <br>
      var buf = [];
      while (i < lines.length && !RE_BLANK.test(lines[i]) && (buf.length === 0 || !isBlockStart(lines, i))) {
        buf.push(lines[i].replace(/[ \t]+$/, ''));
        i++;
      }
      out.push('<p class="md-p">' + buf.map(inline).join('<br>') + '</p>');
    }
    return out.join('\n');
  }

  /* ================================================================== *
   * 4. 盘古之白的 HTML 落地：只改文本节点，跳过标签、属性、代码
   * ================================================================== */

  var RE_BLOCK_TAG = /^<\/?(?:p|div|br|hr|li|ul|ol|table|thead|tbody|tfoot|tr|td|th|h[1-6]|blockquote|pre|code|section|article|header|footer|figure|figcaption|dl|dt|dd|main|aside|nav|form)\b/i;

  function panguHtml(html) {
    var parts = String(html).split(/(<[^>]*>)/);
    var n = parts.length;
    var isTag = new Array(n);
    var isCode = new Array(n);
    var isBlock = new Array(n);
    var edge = new Array(n);
    var depth = 0;
    var i, k;

    for (i = 0; i < n; i++) {
      var p = parts[i];
      if (p.length >= 3 && p.charAt(0) === '<' && p.charAt(p.length - 1) === '>') {
        isTag[i] = true;
        if (/^<(pre|code)\b/i.test(p)) depth++;
        else if (/^<\/(pre|code)\s*>$/i.test(p)) depth = depth > 0 ? depth - 1 : 0;
        isBlock[i] = RE_BLOCK_TAG.test(p);
      } else {
        var a = -1, b = -1;
        for (k = 0; k < p.length; k++) { if (!/\s/.test(p.charAt(k))) { a = k; break; } }
        for (k = p.length - 1; k >= 0; k--) { if (!/\s/.test(p.charAt(k))) { b = k; break; } }
        isCode[i] = depth > 0;
        edge[i] = a < 0 ? null : [p.charAt(a), p.charAt(b)];
      }
    }

    var out = '';
    var prev = '';        // 上一个可见字符（行内标签之间会跨过去，块级标签会打断）
    var tailSpace = false; // 上一段文本是否已经以空白结尾（已有空格就不再补）
    var runStart = -1;    // 紧邻当前文本的「行内标签串」在 out 中的起点
    var runClosing = false;
    for (i = 0; i < n; i++) {
      if (isTag[i]) {
        if (isBlock[i]) { out += parts[i]; prev = ''; tailSpace = false; runStart = -1; continue; }
        if (runStart < 0) { runStart = out.length; runClosing = true; }
        if (parts[i].charAt(1) !== '/') runClosing = false; // 出现开标签 → 空格补在标签串之前
        out += parts[i];
        continue;
      }
      if (isCode[i]) { out += parts[i]; prev = ''; tailSpace = false; runStart = -1; continue; }
      if (!edge[i]) { out += parts[i]; if (/\s/.test(parts[i])) tailSpace = true; continue; }

      var first = edge[i][0];
      // 只在「汉字/中文标点 → 字母」与「字母 → 汉字」两种交界处补空格；
      // 字母后面紧跟中文标点（如 "https://x。结束"）不补，避免标点前多出空格。
      var pad = !tailSpace && !/^\s/.test(parts[i])
        && ((isWide(prev) && isLatin(first)) || (isLatin(prev) && isIdeo(first)));
      if (pad) {
        if (runStart >= 0 && !runClosing) out = out.slice(0, runStart) + ' ' + out.slice(runStart);
        else out += ' ';
      }
      out += pangu(parts[i]);
      prev = edge[i][1];
      tailSpace = /\s$/.test(parts[i]);
      runStart = -1;
    }
    return out;
  }

  /* ================================================================== *
   * 5. 完整渲染
   * ================================================================== */

  function render(text) {
    if (typeof text !== 'string') return '';
    var src = text.replace(/\r\n?/g, '\n').replace(/\u0000/g, '');
    if (!src.trim()) return '';
    return panguHtml(parseBlocks(src.split('\n')));
  }

  /* ================================================================== *
   * 6. 纯文本：剥掉全部 Markdown 标记
   * ================================================================== */

  function plain(text) {
    var src = toText(text);
    if (!src.trim()) return '';
    var lines = src.replace(/\r\n?/g, '\n').split('\n');
    var kept = [];
    var fenceChar = '';
    var fenceLen = 0;
    var i;

    for (i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!fenceChar) {
        var fm = RE_FENCE.exec(line);
        if (fm) { fenceChar = fm[1].charAt(0); fenceLen = fm[1].length; continue; } // 围栏本身不是内容
      } else {
        var closeRe = new RegExp('^ {0,3}' + (fenceChar === '`' ? '`' : '~') + '{' + fenceLen + ',}[ \\t]*$');
        if (closeRe.test(line)) { fenceChar = ''; continue; }
        kept.push(line); // 代码内容原样保留（不再当 Markdown 解析）
        continue;
      }
      if (RE_HR.test(line)) continue;
      if (line.indexOf('|') >= 0 && isDelimRow(line)) continue; // 表格分隔行
      var l = line
        .replace(/^ {0,3}>[ \t]?/, '')
        .replace(/^ {0,3}#{1,6}[ \t]+/, '')
        .replace(/^[ \t]*([-*+]|\d{1,9}[.)])[ \t]+/, '');
      if (l.indexOf('|') >= 0) l = splitRow(l).join(' ');
      kept.push(l);
    }

    var t = kept.join('\n');
    t = t.replace(/!\[([^\]]*)\]\(([^)]*)\)/g, '[图片: $1]');
    t = t.replace(/\[([^\]]*)\]\(([^)]*)\)/g, '$1');
    t = t.replace(/`+([\s\S]*?)`+/g, '$1');
    t = t.replace(/\*\*\*([\s\S]+?)\*\*\*/g, '$1');
    t = t.replace(/___([\s\S]+?)___/g, '$1');
    t = t.replace(/\*\*([\s\S]+?)\*\*/g, '$1');
    t = t.replace(/__([\s\S]+?)__/g, '$1');
    t = t.replace(/~~([\s\S]+?)~~/g, '$1');
    t = t.replace(/(?<![A-Za-z0-9*])\*([^*\n]+?)\*(?![A-Za-z0-9*])/g, '$1');
    t = t.replace(/(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])/g, '$1');
    t = t.replace(/\\([\\`*_{}\[\]()#+\-.!~>|])/g, '$1');
    t = t.replace(/\s+/g, ' ').trim();
    return t ? pangu(t) : '';
  }

  /* ================================================================== */

  window.MD = { render: render, inline: inline, plain: plain };
})();
