/* A small, safe Markdown renderer.
 *
 * Model output is Markdown, so the chat has to render it: headings, bold,
 * lists, tables, code. The renderer escapes every byte of the source first,
 * then adds structure, so a model can never inject markup into the shell.
 *
 * Supported: ATX headings, fenced code (``` and ~~~, any info string, an
 * unclosed fence renders as code so streaming looks right), nested unordered
 * and ordered lists, task items, GFM pipe tables (with alignment and escaped
 * pipes), blockquotes (recursive), horizontal rules, paragraphs, and inline
 * bold, italic, strikethrough, code, links and bare http(s) autolinks.
 *
 * Reasoning: splitReasoning() separates <think>/<thinking>/<reasoning>
 * blocks from the visible answer. It is shared by the live stream and the
 * transcript loader, so a reloaded session renders exactly like a live one.
 */
(function () {
  'use strict';

  var SAFE_LINK = /^(https?:\/\/|mailto:)/i;
  var AMP = String.fromCharCode(38);
  var LT = String.fromCharCode(60);
  var GT = String.fromCharCode(62);
  var QUOT = String.fromCharCode(34);
  var APOS = String.fromCharCode(39);
  /* A marker no model is likely to emit, built at runtime so the file stays
   * plain text. */
  var MARK = String.fromCharCode(1);
  var MARK_RE = new RegExp(MARK + '(CODE|LINK)(\\d+)' + MARK, 'g');

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .split(AMP).join(AMP + 'amp;')
      .split(LT).join(AMP + 'lt;')
      .split(GT).join(AMP + 'gt;')
      .split(QUOT).join(AMP + 'quot;')
      .split(APOS).join(AMP + '#39;');
  }

  function inline(text) {
    var out = escapeHtml(text);
    var codeParts = [];
    var linkParts = [];

    /* inline code first: its content must not be touched by the rest */
    out = out.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, function (match, ticks, body) {
      codeParts.push(body.replace(/^ (.*) $/, '$1'));
      return MARK + 'CODE' + (codeParts.length - 1) + MARK;
    });

    /* images: the CSP blocks remote images (img-src 'self' data:), so an
     * image becomes an honest link chip instead of a broken picture. */
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, function (match, alt, href) {
      var label = 'Image' + (alt ? ': ' + alt : '');
      if (!SAFE_LINK.test(href)) return '<span class="md-image">' + label + '</span>';
      linkParts.push('<a class="md-image" href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>');
      return MARK + 'LINK' + (linkParts.length - 1) + MARK;
    });

    /* links: only http, https and mailto survive */
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, function (match, label, href) {
      if (!SAFE_LINK.test(href)) return label;
      linkParts.push('<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>');
      return MARK + 'LINK' + (linkParts.length - 1) + MARK;
    });

    /* bare URLs become links; trailing punctuation stays outside */
    out = out.replace(/(^|[\s(])(https?:\/\/(?:(?!&quot;|&#39;|&gt;)[^\s<])*(?:(?!&quot;|&#39;|&gt;)[^\s<.,;:!?)\]]))/g, function (match, lead, url) {
      linkParts.push('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>');
      return lead + MARK + 'LINK' + (linkParts.length - 1) + MARK;
    });

    out = out.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    out = out.replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,;:!?])/g, '$1<em>$2</em>');
    out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');

    out = out.replace(MARK_RE, function (match, kind, index) {
      if (kind === 'CODE') {
        return '<code class="md-code-inline">' + codeParts[Number(index)] + '</code>';
      }
      return linkParts[Number(index)];
    });
    return out;
  }

  /* ---------------------------------------------------------------- blocks */

  var FENCE_OPEN = /^(\s{0,3})(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
  var LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  function isTableSeparator(line) {
    var value = line || '';
    /* A lone "---" is a rule, not a one-column table separator. */
    if (value.indexOf('|') === -1) return false;
    return /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(value);
  }

  function isTableStart(lines, index) {
    return lines[index].indexOf('|') !== -1 && index + 1 < lines.length && isTableSeparator(lines[index + 1]);
  }

  /* Split a row on unescaped pipes that are not inside inline code. */
  function splitRow(line) {
    var trimmed = line.trim();
    if (trimmed.charAt(0) === '|') trimmed = trimmed.slice(1);
    if (trimmed.charAt(trimmed.length - 1) === '|' && trimmed.charAt(trimmed.length - 2) !== '\\') {
      trimmed = trimmed.slice(0, -1);
    }
    var cells = [];
    var cell = '';
    var inCode = false;
    for (var i = 0; i < trimmed.length; i += 1) {
      var ch = trimmed.charAt(i);
      if (ch === '\\' && trimmed.charAt(i + 1) === '|') {
        cell += '|';
        i += 1;
        continue;
      }
      if (ch === '`') inCode = !inCode;
      if (ch === '|' && !inCode) {
        cells.push(cell.trim());
        cell = '';
        continue;
      }
      cell += ch;
    }
    cells.push(cell.trim());
    return cells;
  }

  function alignments(separator) {
    return splitRow(separator).map(function (cell) {
      var left = cell.charAt(0) === ':';
      var right = cell.charAt(cell.length - 1) === ':';
      if (left && right) return 'md-al-c';
      if (right) return 'md-al-r';
      return '';
    });
  }

  function cellTag(tag, content, align) {
    return '<' + tag + (align ? ' class="' + align + '"' : '') + '>' + inline(content) + '</' + tag + '>';
  }

  function isHr(line) {
    return /^\s{0,3}([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line) && !/[^\s\-*_]/.test(line);
  }

  function isBlockStart(lines, index) {
    var line = lines[index];
    if (/^\s*(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|\$\$|\\\[)/.test(line)) return true;
    if (FENCE_OPEN.test(line)) return true;
    if (isHr(line)) return true;
    return isTableStart(lines, index);
  }

  function indentOf(text) {
    var match = /^(\s*)/.exec(text.replace(/\t/g, '    '));
    return match ? match[1].length : 0;
  }

  /* Parse a (possibly nested) list starting at `start`. Returns html + next. */
  function parseList(lines, start) {
    var first = LIST_ITEM.exec(lines[start]);
    var baseIndent = indentOf(first[1]);
    var ordered = /\d/.test(first[2]);
    var items = [];
    var index = start;

    while (index < lines.length) {
      var line = lines[index];
      if (!line.trim()) {
        /* A blank line ends the list unless the next line continues it. */
        var next = lines[index + 1];
        var nextMatch = next !== undefined ? LIST_ITEM.exec(next) : null;
        if (nextMatch && indentOf(nextMatch[1]) >= baseIndent) {
          index += 1;
          continue;
        }
        break;
      }
      var match = LIST_ITEM.exec(line);
      var indent = indentOf(line);
      if (match && indent === baseIndent) {
        if (/\d/.test(match[2]) !== ordered) break;
        items.push({ text: match[3], children: '' });
        index += 1;
        continue;
      }
      if (match && indent > baseIndent && items.length) {
        var nested = parseList(lines, index);
        items[items.length - 1].children += nested.html;
        index = nested.next;
        continue;
      }
      if (!match && indent > baseIndent && items.length) {
        /* lazy continuation of the previous item */
        items[items.length - 1].text += ' ' + line.trim();
        index += 1;
        continue;
      }
      break;
    }

    var startAttr = '';
    if (ordered) {
      var number = parseInt(first[2], 10);
      if (number && number !== 1) startAttr = ' start="' + number + '"';
    }
    var html = (ordered ? '<ol class="md-list"' + startAttr + '>' : '<ul class="md-list">') +
      items.map(function (item) {
        var task = /^\[([ xX])\]\s+(.*)$/.exec(item.text);
        if (task) {
          var done = task[1] !== ' ';
          return '<li class="md-task' + (done ? ' is-done' : '') + '">' +
            '<span class="md-check" aria-hidden="true">' + (done ? '&#10003;' : '') + '</span>' +
            '<span class="sr-only">' + (done ? 'Done: ' : 'To do: ') + '</span>' +
            inline(task[2]) + item.children + '</li>';
        }
        return '<li>' + inline(item.text) + item.children + '</li>';
      }).join('') +
      (ordered ? '</ol>' : '</ul>');
    return { html: html, next: index };
  }

  function render(source) {
    var lines = String(source == null ? '' : source).replace(/\r\n?/g, '\n').split('\n');
    var html = [];
    var index = 0;

    while (index < lines.length) {
      var line = lines[index];

      if (!line.trim()) {
        index += 1;
        continue;
      }

      /* fenced code; an unclosed fence runs to the end (streaming) */
      var fence = FENCE_OPEN.exec(line);
      if (fence) {
        var marker = fence[2];
        var language = fence[3] || '';
        var closeRe = new RegExp('^\\s{0,3}' + (marker.charAt(0) === '`' ? '`' : '~') + '{' + marker.length + ',}\\s*$');
        var body = [];
        var closed = false;
        index += 1;
        while (index < lines.length) {
          if (closeRe.test(lines[index])) {
            closed = true;
            break;
          }
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        html.push(
          '<div class="md-codeblock' + (closed ? '' : ' is-open') + '">' +
          '<div class="md-codehead">' +
          '<span class="md-lang">' + escapeHtml(language || 'code') + '</span>' +
          '<button class="md-copy" type="button" data-md-copy aria-label="Copy code">Copy</button>' +
          '</div>' +
          '<pre class="md-code" tabindex="0"><code>' + escapeHtml(body.join('\n')) + '</code></pre>' +
          '</div>'
        );
        continue;
      }

      /* display math: $$ ... $$ or \[ ... \] (shown as source; no math
       * typesetter ships with the shell) */
      var mathOpen = /^\s*(\$\$|\\\[)\s*(.*)$/.exec(line);
      if (mathOpen) {
        var closer = mathOpen[1] === '$$' ? '$$' : '\\]';
        var mathBody = [];
        var rest = mathOpen[2];
        var mathClosed = false;
        if (rest && rest.slice(-closer.length) === closer) {
          mathBody.push(rest.slice(0, -closer.length));
          mathClosed = true;
          index += 1;
        } else {
          if (rest) mathBody.push(rest);
          index += 1;
          while (index < lines.length) {
            var mline = lines[index];
            index += 1;
            var at = mline.indexOf(closer);
            if (at !== -1) {
              if (mline.slice(0, at).trim()) mathBody.push(mline.slice(0, at));
              mathClosed = true;
              break;
            }
            mathBody.push(mline);
          }
        }
        html.push('<div class="md-math' + (mathClosed ? '' : ' is-open') + '" role="math" aria-label="Formula">' +
          '<span class="md-math-tag" aria-hidden="true">formula</span><code>' + escapeHtml(mathBody.join('\n').trim()) + '</code></div>');
        continue;
      }

      /* heading */
      var heading = line.match(/^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/);
      if (heading) {
        var level = Math.min(6, heading[1].length + 1);
        html.push('<h' + level + ' class="md-h">' + inline(heading[2]) + '</h' + level + '>');
        index += 1;
        continue;
      }

      /* horizontal rule */
      if (isHr(line)) {
        html.push('<hr class="md-rule">');
        index += 1;
        continue;
      }

      /* table */
      if (isTableStart(lines, index)) {
        var head = splitRow(line);
        var align = alignments(lines[index + 1]);
        index += 2;
        var rows = [];
        while (index < lines.length && lines[index].indexOf('|') !== -1 && lines[index].trim()) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        var table = ['<div class="md-table-wrap" tabindex="0"><table class="md-table"><thead><tr>'];
        head.forEach(function (cell, column) { table.push(cellTag('th', cell, align[column])); });
        table.push('</tr></thead><tbody>');
        rows.forEach(function (row) {
          table.push('<tr>');
          for (var column = 0; column < head.length; column += 1) {
            table.push(cellTag('td', row[column] || '', align[column]));
          }
          table.push('</tr>');
        });
        table.push('</tbody></table></div>');
        html.push(table.join(''));
        continue;
      }

      /* blockquote, rendered recursively */
      if (/^\s{0,3}>/.test(line)) {
        var quote = [];
        while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s{0,3}>\s?/, ''));
          index += 1;
        }
        /* GitHub-style callouts: > [!NOTE], [!TIP], [!IMPORTANT],
         * [!WARNING], [!CAUTION] */
        var callout = /^\s*\[!(note|tip|important|warning|caution)\]\s*$/i.exec(quote[0] || '');
        if (callout) {
          var kind = callout[1].toLowerCase();
          html.push('<aside class="md-callout md-callout--' + kind + '" role="note">' +
            '<span class="md-callout-title">' + kind.charAt(0).toUpperCase() + kind.slice(1) + '</span>' +
            render(quote.slice(1).join('\n')) + '</aside>');
          continue;
        }
        html.push('<blockquote class="md-quote">' + render(quote.join('\n')) + '</blockquote>');
        continue;
      }

      /* lists (nested by indentation) */
      if (LIST_ITEM.test(line)) {
        var list = parseList(lines, index);
        html.push(list.html);
        index = list.next;
        continue;
      }

      /* paragraph */
      var paragraph = [];
      while (index < lines.length && lines[index].trim() && (!paragraph.length || !isBlockStart(lines, index))) {
        paragraph.push(lines[index]);
        index += 1;
      }
      html.push('<p class="md-p">' + inline(paragraph.join('\n')).replace(/\n/g, '<br />') + '</p>');
    }

    return html.join('');
  }

  /* -------------------------------------------------------------- reasoning */

  var TAG_RE = /<(\/?)(think|thinking|reasoning)>/gi;
  var PARTIAL_TAG = /<\/?(t(h(i(n(k(i(n(g)?)?)?)?)?)?)?|r(e(a(s(o(n(i(n(g)?)?)?)?)?)?)?)?)?$/i;

  /* Split raw model text into its reasoning trace and its visible answer.
   *
   * Handles: several blocks; an unclosed block (still thinking); a close tag
   * with no open tag (templates that put <think> in the prompt, so the
   * output starts inside the block); and, while streaming, a half-received
   * tag at the very end (`<thi`) which is held back instead of flashing in
   * the answer. Tags may arrive split across tokens, so callers always pass
   * the accumulated text. */
  function splitReasoning(raw, options) {
    var text = String(raw || '');
    var streaming = Boolean(options && options.streaming);
    var reasoning = [];
    var answer = [];
    var inside = false;
    var seen = false;
    var last = 0;
    var match;

    TAG_RE.lastIndex = 0;
    var firstClose = text.search(/<\/(think|thinking|reasoning)>/i);
    var firstOpen = text.search(/<(think|thinking|reasoning)>/i);
    if (firstClose !== -1 && (firstOpen === -1 || firstClose < firstOpen)) {
      inside = true; /* output began inside an implicit block */
    }

    while ((match = TAG_RE.exec(text)) !== null) {
      var chunk = text.slice(last, match.index);
      if (inside) reasoning.push(chunk); else answer.push(chunk);
      inside = match[1] !== '/';
      seen = true;
      last = TAG_RE.lastIndex;
    }
    var tail = text.slice(last);
    if (streaming) {
      var partial = PARTIAL_TAG.exec(tail);
      if (partial && partial[0].length > 1) tail = tail.slice(0, partial.index);
    }
    if (inside) reasoning.push(tail); else answer.push(tail);

    return {
      reasoning: reasoning.join('\n').replace(/^\s+|\s+$/g, ''),
      answer: answer.join('').replace(/^\s+/, ''),
      thinking: inside,
      hasReasoning: seen
    };
  }

  window.__imurun_markdown = {
    render: render,
    escape: escapeHtml,
    inline: inline,
    splitReasoning: splitReasoning
  };
})();
