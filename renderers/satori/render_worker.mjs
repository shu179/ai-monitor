import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

import { Resvg } from "@resvg/resvg-js";
import MarkdownIt from "markdown-it";
import React from "react";
import satori from "satori";
import sharp from "sharp";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "../..");
const md = new MarkdownIt({
  html: false,
  linkify: false,
  typographer: false,
});

const WIDTH = 900;
const BASE_DENSITY = 72;
const MAX_RASTER_HEIGHT = 28000;
const MIN_VIEWPORT_HEIGHT = 800;
const BODY_PADDING_TOP = 80;
const BODY_PADDING_BOTTOM = 60;
const CARD_WIDTH = 760;
const CARD_PADDING_X = 48;
const ANSWER_WIDTH = 604;
const BG = "#fafafa";
const TEXT_DARK = "#0f172a";
const TEXT_BODY = "#1e293b";
const TEXT_LIGHT = "#94a3b8";
const CYAN = "#0ea5e9";
const HEADING_FONT_SIZES = { 1: 30, 2: 24, 3: 20, 4: 18, 5: 16, 6: 16 };
const FIRST_HEADING_TOP_MARGIN = 0;
const EMOJI_IMAGE_DIR = path.join(__dirname, "node_modules", "emoji-datasource-twitter", "img", "twitter", "64");
const EMOJI_DATA_PATH = path.join(__dirname, "node_modules", "emoji-datasource-twitter", "emoji.json");
const emojiImageCache = new Map();
const emojiImageFileNames = loadEmojiImageFileNames();
const emojiImageByChar = loadEmojiImageByChar();

function h(type, props, ...children) {
  return React.createElement(type, props || {}, ...children.flat().filter((child) => child !== null && child !== undefined && child !== ""));
}

function readAsset(relPath) {
  return fs.readFileSync(path.join(repoRoot, relPath));
}

function toArrayBuffer(buffer) {
  return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
}

function loadFonts() {
  const candidates = [
    ["Inter", "assets/fonts/inter-400.ttf", 400],
    ["Inter", "assets/fonts/inter-500.ttf", 500],
    ["Newsreader", "assets/fonts/newsreader-400.ttf", 400],
    ["JetBrains Mono", "assets/fonts/jetbrains-mono-400.ttf", 400],
    ["Source Han Sans CN", "assets/fonts/source-han-sans-cn-400.otf", 400],
    ["Source Han Sans CN", "assets/fonts/source-han-sans-cn-500.otf", 500],
  ];
  return candidates
    .filter(([, relPath]) => fs.existsSync(path.join(repoRoot, relPath)))
    .map(([name, relPath, weight]) => ({
      name,
      data: toArrayBuffer(readAsset(relPath)),
      weight,
      style: "normal",
    }));
}

const fonts = loadFonts();

function dataUriForLogo(platform) {
  const safePlatform = String(platform || "").replace(/[^a-zA-Z0-9_-]/g, "");
  if (!safePlatform) return "";
  for (const suffix of [".png", ".jpg", ".jpeg", ".webp", ".svg"]) {
    const filePath = path.join(repoRoot, "assets", "logos", `${safePlatform}${suffix}`);
    if (!fs.existsSync(filePath)) continue;
    const mime = suffix === ".svg" ? "image/svg+xml" : suffix === ".webp" ? "image/webp" : suffix === ".jpg" || suffix === ".jpeg" ? "image/jpeg" : "image/png";
    return `data:${mime};base64,${fs.readFileSync(filePath).toString("base64")}`;
  }
  return "";
}

function emojiKey(segment) {
  return Array.from(String(segment || ""))
    .map((ch) => ch.codePointAt(0).toString(16))
    .join("-");
}

function loadEmojiImageFileNames() {
  try {
    return new Set(fs.readdirSync(EMOJI_IMAGE_DIR).filter((name) => name.endsWith(".png")));
  } catch {
    return new Set();
  }
}

function emojiFromUnified(unified) {
  const parts = String(unified || "")
    .split("-")
    .map((part) => Number.parseInt(part, 16))
    .filter((code) => Number.isFinite(code));
  if (!parts.length) return "";
  try {
    return String.fromCodePoint(...parts);
  } catch {
    return "";
  }
}

function loadEmojiImageByChar() {
  const out = new Map();
  try {
    const entries = JSON.parse(fs.readFileSync(EMOJI_DATA_PATH, "utf8"));
    for (const entry of entries || []) {
      if (!entry?.has_img_twitter || !entry.image) continue;
      for (const unified of [entry.unified, entry.non_qualified]) {
        const emoji = emojiFromUnified(unified);
        if (emoji) out.set(emoji, entry.image);
      }
      for (const variation of Object.values(entry.skin_variations || {})) {
        if (!variation?.has_img_twitter || !variation.image) continue;
        for (const unified of [variation.unified, variation.non_qualified]) {
          const emoji = emojiFromUnified(unified);
          if (emoji) out.set(emoji, variation.image);
        }
      }
    }
  } catch {
    // The renderer still works for text if emoji metadata is unavailable.
  }
  return out;
}

function emojiCandidateFileNames(segment) {
  const mapped = emojiImageByChar.get(String(segment || ""));
  const key = emojiKey(segment);
  if (!key) return [];
  const withoutEmojiVariation = key.replace(/-fe0f/g, "").replace(/fe0f-/g, "");
  const withoutTextVariation = key.replace(/-fe0e/g, "").replace(/fe0e-/g, "");
  const candidates = [
    mapped,
    `${key}.png`,
    `${withoutEmojiVariation}.png`,
    `${withoutTextVariation}.png`,
  ].filter(Boolean);
  return [...new Set(candidates)];
}

function emojiImageFileName(segment) {
  for (const fileName of emojiCandidateFileNames(segment)) {
    if (emojiImageFileNames.has(fileName)) return fileName;
  }
  return "";
}

function emojiImageDataUri(segment) {
  const key = String(segment || "");
  if (!key) return "";
  if (emojiImageCache.has(key)) return emojiImageCache.get(key);

  for (const fileName of emojiCandidateFileNames(key)) {
    if (!emojiImageFileNames.has(fileName)) continue;
    const filePath = path.join(EMOJI_IMAGE_DIR, fileName);
    const uri = `data:image/png;base64,${fs.readFileSync(filePath).toString("base64")}`;
    emojiImageCache.set(key, uri);
    return uri;
  }

  emojiImageCache.set(key, "");
  return "";
}

const platformLabels = {
  local_model: "本地模型",
  doubao: "豆包",
  deepseek: "DeepSeek",
  ark_deepseek: "方舟 DeepSeek",
  kimi: "Kimi",
  yuanbao: "腾讯元宝",
  tongyi: "通义千问",
  wenxin: "文心一言",
};

function normalizeCopiedMarkdownArtifacts(text) {
  return String(text || "").replace(
    /\*\*((?:[“‘「『《"][^*\n]{1,80}[”’」』》"][^*\n]{0,24}))\*\*/g,
    "$1",
  );
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function compileBrandPattern(brand) {
  const tokens = String(brand || "").trim().split(/\s+/).filter(Boolean);
  if (!tokens.length) return null;
  try {
    return new RegExp(tokens.map(escapeRegExp).join("\\s*"), "gi");
  } catch {
    return null;
  }
}

function addBreakOpportunities(value) {
  return String(value || "").replace(/([A-Za-z0-9_./:?=&%#-]{28})(?=[A-Za-z0-9_./:?=&%#-])/g, "$1\u200b");
}

function mayStartEmoji(ch, next) {
  const code = String(ch || "").codePointAt(0);
  if (!Number.isFinite(code)) return false;
  if (code >= 0x1f000 && code <= 0x1faff) return true;
  if (code >= 0x2600 && code <= 0x27bf) return true;
  if (code >= 0x2300 && code <= 0x23ff) return true;
  if (code >= 0x2b00 && code <= 0x2bff) return true;
  if ([0x00a9, 0x00ae, 0x203c, 0x2049, 0x2122, 0x2139, 0x3030, 0x303d, 0x3297, 0x3299].includes(code)) return true;
  return /[#*0-9]/.test(ch) && (next === "\ufe0f" || next === "\u20e3");
}

function findEmojiFragment(chars, index) {
  if (!mayStartEmoji(chars[index], chars[index + 1])) return null;
  const maxLength = Math.min(12, chars.length - index);
  for (let length = maxLength; length > 0; length -= 1) {
    const value = chars.slice(index, index + length).join("");
    const src = emojiImageDataUri(value);
    if (src) return { type: "emoji", value, src, length };
    if (emojiImageFileName(value)) return { type: "emoji", value, src: emojiImageDataUri(value), length };
  }
  return null;
}

function textFragments(value) {
  const out = [];
  const source = String(value || "");
  const chars = Array.from(source);
  let latinRun = "";
  const flushLatin = () => {
    if (latinRun) {
      out.push(addBreakOpportunities(latinRun));
      latinRun = "";
    }
  };

  for (let i = 0; i < chars.length; i += 1) {
    const ch = chars[i];
    const emoji = findEmojiFragment(chars, i);
    if (emoji) {
      flushLatin();
      out.push(emoji);
      i += emoji.length - 1;
      continue;
    }
    if (/[A-Za-z0-9_./:?=&%#-]/.test(ch)) {
      latinRun += ch;
      if (latinRun.length >= 18) {
        flushLatin();
      }
    } else {
      flushLatin();
      out.push(ch);
    }
  }
  flushLatin();
  return out;
}

function emojiSizeFromStyle(inheritedStyle = {}) {
  const raw = inheritedStyle.fontSize;
  const fontSize = typeof raw === "number" ? raw : Number.parseFloat(String(raw || ""));
  return Math.max(16, Math.round((Number.isFinite(fontSize) ? fontSize : 15) * 1.18));
}

function renderTextFragment(part, key, inheritedStyle = {}) {
  if (part && typeof part === "object" && part.type === "emoji" && part.src) {
    const size = emojiSizeFromStyle(inheritedStyle);
    return h("img", {
      key,
      src: part.src,
      style: {
        width: size,
        height: size,
        objectFit: "contain",
        flexShrink: 0,
        alignSelf: "center",
        marginLeft: 1,
        marginRight: 1,
      },
    });
  }
  return h("span", { key, style: inheritedStyle }, part);
}

function renderTextSpan(text, pattern, key, inheritedStyle = {}) {
  const value = String(text || "");
  if (!value) return [];
  if (!pattern) {
    return textFragments(value).map((part, index) => renderTextFragment(part, `${key}-${index}`, inheritedStyle));
  }

  const out = [];
  let lastIndex = 0;
  let index = 0;
  pattern.lastIndex = 0;
  for (const match of value.matchAll(pattern)) {
    const start = match.index || 0;
    let matchEnd = start + match[0].length;
    let highlightText = match[0].replace(/\s+/g, " ");
    while (matchEnd < value.length && /[、，。；：！？）)]/.test(value[matchEnd])) {
      highlightText += value[matchEnd];
      matchEnd += 1;
    }
    if (start > lastIndex) {
      for (const part of textFragments(value.slice(lastIndex, start))) {
        out.push(renderTextFragment(part, `${key}-t-${index++}`, inheritedStyle));
      }
    }
    out.push(
      h(
        "span",
        {
          key: `${key}-h-${index++}`,
          style: {
            ...inheritedStyle,
            backgroundColor: "rgba(14, 165, 233, 0.1)",
            color: "#0284c7",
            borderRadius: 4,
            fontWeight: 500,
          },
        },
        addBreakOpportunities(highlightText),
      ),
    );
    lastIndex = matchEnd;
  }
  if (lastIndex < value.length) {
    for (const part of textFragments(value.slice(lastIndex))) {
      out.push(renderTextFragment(part, `${key}-t-${index++}`, inheritedStyle));
    }
  }
  if (!out.length) {
    for (const part of textFragments(value)) {
      out.push(renderTextFragment(part, `${key}-t-${index++}`, inheritedStyle));
    }
  }
  return out;
}

function findMatchingInlineClose(tokens, start, openType, closeType) {
  let depth = 0;
  for (let i = start; i < tokens.length; i += 1) {
    if (tokens[i].type === openType) depth += 1;
    if (tokens[i].type === closeType) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return start;
}

function renderInlineRange(tokens, start, end, pattern, keyPrefix, inheritedStyle = {}) {
  const out = [];
  for (let i = start; i < end; i += 1) {
    const token = tokens[i];
    const key = `${keyPrefix}-${i}`;
    if (token.type === "text") {
      out.push(...renderTextSpan(token.content, pattern, key, inheritedStyle));
    } else if (token.type === "html_inline") {
      out.push(...renderTextSpan(token.content, pattern, key, inheritedStyle));
    } else if (token.type === "image") {
      continue;
    } else if (token.type === "code_inline") {
      out.push(
        h(
          "span",
          {
            key,
            style: {
              ...inheritedStyle,
              fontFamily: "JetBrains Mono",
              fontSize: 13,
              backgroundColor: "rgba(15, 23, 42, 0.05)",
              borderRadius: 4,
              padding: "2px 6px",
            },
          },
          token.content,
        ),
      );
    } else if (token.type === "strong_open") {
      const close = findMatchingInlineClose(tokens, i, "strong_open", "strong_close");
      out.push(...renderInlineRange(tokens, i + 1, close, pattern, key, { ...inheritedStyle, color: TEXT_DARK, fontWeight: 600 }));
      i = close;
    } else if (token.type === "em_open") {
      const close = findMatchingInlineClose(tokens, i, "em_open", "em_close");
      out.push(...renderInlineRange(tokens, i + 1, close, pattern, key, inheritedStyle));
      i = close;
    } else if (token.type === "link_open") {
      const close = findMatchingInlineClose(tokens, i, "link_open", "link_close");
      out.push(...renderInlineRange(tokens, i + 1, close, pattern, key, { ...inheritedStyle, color: "#0369a1" }));
      i = close;
    } else if (token.type === "softbreak" || token.type === "hardbreak") {
      out.push(h("span", { key, style: inheritedStyle }, " "));
    }
  }
  return out;
}

function renderInline(token, pattern, keyPrefix, inheritedStyle = {}) {
  return renderInlineRange(token?.children || [], 0, token?.children?.length || 0, pattern, keyPrefix, inheritedStyle);
}

function splitInlineChildrenByBreaks(children) {
  const segments = [[]];
  for (const child of children || []) {
    if (child.type === "softbreak" || child.type === "hardbreak") {
      segments.push([]);
      continue;
    }
    segments[segments.length - 1].push(child);
  }
  return segments.filter((segment) => segment.length);
}

function renderInlineLines(token, pattern, keyPrefix, inheritedStyle = {}) {
  const segments = splitInlineChildrenByBreaks(token?.children || []);
  if (!segments.length) return [];
  return segments.map((segment, index) =>
    h(
      "div",
      {
        key: `${keyPrefix}-line-${index}`,
        style: {
          display: "flex",
          flexWrap: "wrap",
          width: "100%",
          ...baseTextStyle(inheritedStyle),
        },
      },
      renderInlineRange(segment, 0, segment.length, pattern, `${keyPrefix}-line-${index}`, inheritedStyle),
    ),
  );
}

function renderPlainTextLines(text, pattern, keyPrefix, inheritedStyle = {}) {
  return String(text || "")
    .split(/\r?\n/)
    .filter((line) => line.length)
    .map((line, index) =>
      h(
        "div",
        {
          key: `${keyPrefix}-line-${index}`,
          style: {
            display: "flex",
            flexWrap: "wrap",
            width: "100%",
            ...baseTextStyle(inheritedStyle),
          },
        },
        renderTextSpan(line, pattern, `${keyPrefix}-line-${index}`, inheritedStyle),
      ),
    );
}

function findMatchingBlockClose(tokens, start, openType, closeType) {
  let depth = 0;
  for (let i = start; i < tokens.length; i += 1) {
    if (tokens[i].type === openType) depth += 1;
    if (tokens[i].type === closeType) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return start;
}

function baseTextStyle(extra = {}) {
  return {
    color: TEXT_BODY,
    fontFamily: "Source Han Sans CN",
    fontSize: 15,
    lineHeight: 1.75,
    letterSpacing: 0,
    ...extra,
  };
}

function headingMetrics(level, options = {}) {
  const fontSize = HEADING_FONT_SIZES[level] || 18;
  const firstBlock = Boolean(options.firstBlock);
  return {
    fontSize,
    lineHeight: firstBlock ? 1.18 : 1.35,
    marginTop: firstBlock ? FIRST_HEADING_TOP_MARGIN : 18,
    marginBottom: 10,
  };
}

function headingStyle(level, options = {}) {
  const metrics = headingMetrics(level, options);
  return {
    display: "flex",
    flexWrap: "wrap",
    marginTop: metrics.marginTop,
    marginBottom: metrics.marginBottom,
    color: TEXT_DARK,
    fontFamily: "Source Han Sans CN",
    fontSize: metrics.fontSize,
    fontWeight: level <= 3 ? 500 : 500,
    lineHeight: metrics.lineHeight,
    letterSpacing: 0,
  };
}

function renderBlocks(tokens, start, end, pattern, keyPrefix, options = {}) {
  const out = [];
  let hasRenderedBlock = false;
  for (let i = start; i < end; i += 1) {
    const token = tokens[i];
    const key = `${keyPrefix}-${i}`;
    if (token.type === "heading_open") {
      const level = Number(String(token.tag || "h3").slice(1)) || 3;
      const inline = tokens[i + 1];
      out.push(h("div", { key, style: headingStyle(level, { firstBlock: !hasRenderedBlock }) }, renderInline(inline, pattern, `${key}-in`)));
      hasRenderedBlock = true;
      i += 2;
    } else if (token.type === "paragraph_open") {
      const inline = tokens[i + 1];
      const renderedLines = renderInlineLines(inline, pattern, `${key}-in`);
      if (!renderedLines.length) {
        i += 2;
        continue;
      }
      out.push(
        h(
          "div",
          {
            key,
            style: {
              display: "flex",
              flexDirection: "column",
              marginBottom: options.compactParagraphs ? 0 : 16,
            },
          },
          renderedLines,
        ),
      );
      hasRenderedBlock = true;
      i += 2;
    } else if (token.type === "bullet_list_open" || token.type === "ordered_list_open") {
      const close = findMatchingBlockClose(tokens, i, token.type, token.type === "bullet_list_open" ? "bullet_list_close" : "ordered_list_close");
      out.push(renderList(tokens, i + 1, close, pattern, key, token.type === "ordered_list_open", options));
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "blockquote_open") {
      const close = findMatchingBlockClose(tokens, i, "blockquote_open", "blockquote_close");
      out.push(
        h(
          "div",
          {
            key,
            style: {
              display: "flex",
              flexDirection: "column",
              borderLeft: `3px solid ${CYAN}`,
              paddingLeft: 16,
              marginBottom: 16,
              color: TEXT_LIGHT,
            },
          },
          renderBlocks(tokens, i + 1, close, pattern, `${key}-quote`, options),
        ),
      );
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "fence" || token.type === "code_block") {
      out.push(
        h(
          "div",
          {
            key,
            style: {
              display: "flex",
              flexDirection: "column",
              backgroundColor: "rgba(15, 23, 42, 0.03)",
              border: "1px solid rgba(226, 232, 240, 0.8)",
              borderRadius: 8,
              padding: 16,
              marginBottom: 16,
              fontFamily: "JetBrains Mono",
              fontSize: 13,
              lineHeight: 1.55,
              color: TEXT_BODY,
              whiteSpace: "pre-wrap",
            },
          },
          token.content || "",
        ),
      );
      hasRenderedBlock = true;
    } else if (token.type === "table_open") {
      const close = findMatchingBlockClose(tokens, i, "table_open", "table_close");
      out.push(renderTable(tokens, i + 1, close, pattern, key));
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "hr") {
      out.push(h("div", { key, style: { height: 1, backgroundColor: "rgba(226, 232, 240, 0.8)", marginBottom: 16, display: "flex" } }));
      hasRenderedBlock = true;
    } else if (token.type === "html_block") {
      if (!token.content) {
        continue;
      }
      const renderedLines = renderPlainTextLines(token.content, pattern, `${key}-html`);
      if (!renderedLines.length) {
        continue;
      }
      out.push(
        h(
          "div",
          {
            key,
            style: {
              display: "flex",
              flexDirection: "column",
              marginBottom: options.compactParagraphs ? 0 : 16,
            },
          },
          renderedLines,
        ),
      );
      hasRenderedBlock = true;
    }
  }
  return out;
}

function renderList(tokens, start, end, pattern, keyPrefix, ordered, options = {}) {
  const items = [];
  const compact = Boolean(options.compactParagraphs);
  const listDepth = Number(options.listDepth || 0);
  let itemNumber = 1;
  for (let i = start; i < end; i += 1) {
    if (tokens[i].type !== "list_item_open") continue;
    const close = findMatchingBlockClose(tokens, i, "list_item_open", "list_item_close");
    items.push(
      h(
        "div",
        {
          key: `${keyPrefix}-item-${items.length}`,
          style: {
            display: "flex",
            alignItems: "flex-start",
            marginBottom: compact ? 4 : 10,
          },
        },
        ordered
          ? h(
              "div",
              {
                style: {
                  width: 24,
                  paddingTop: 2,
                  color: TEXT_DARK,
                  fontFamily: "Source Han Sans CN",
                  fontSize: 15,
                  lineHeight: 1.68,
                },
              },
              `${itemNumber}.`,
            )
          : h(
              "div",
              {
                style: {
                  width: 24,
                  height: 26,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "flex-start",
                  paddingTop: 2,
                },
              },
              h("div", {
                style: {
                  width: listDepth > 0 ? 5 : 4,
                  height: listDepth > 0 ? 5 : 4,
                  borderRadius: 999,
                  backgroundColor: listDepth > 0 ? "transparent" : TEXT_DARK,
                  border: listDepth > 0 ? `1px solid ${TEXT_DARK}` : "0px solid transparent",
                },
              }),
            ),
        h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              flex: 1,
              minWidth: 0,
            },
          },
          renderBlocks(tokens, i + 1, close, pattern, `${keyPrefix}-item-${items.length}`, { compactParagraphs: true, listDepth: listDepth + 1 }),
        ),
      ),
    );
    itemNumber += 1;
    i = close;
  }
  return h(
    "div",
    {
      key: keyPrefix,
      style: {
        display: "flex",
        flexDirection: "column",
        marginBottom: compact ? 2 : 10,
      },
    },
    items,
  );
}

function collectTableRows(tokens, start, end) {
  const rows = [];
  for (let i = start; i < end; i += 1) {
    if (tokens[i].type !== "tr_open") continue;
    const row = [];
    let isHeader = false;
    const trClose = findMatchingBlockClose(tokens, i, "tr_open", "tr_close");
    for (let j = i + 1; j < trClose; j += 1) {
      if (tokens[j].type === "th_open" || tokens[j].type === "td_open") {
        isHeader = isHeader || tokens[j].type === "th_open";
        const inline = tokens[j + 1];
        row.push({ children: inline, header: tokens[j].type === "th_open" });
      }
    }
    rows.push({ cells: row, header: isHeader });
    i = trClose;
  }
  return rows;
}

const TABLE_FONT_SIZE = 14;
const TABLE_LINE_HEIGHT = 1.55;
const TABLE_CELL_PADDING_X = 24;
const TABLE_CELL_PADDING_Y = 16;
const TABLE_MIN_ROW_HEIGHT = 42;

function isCjkChar(ch) {
  return /[\u2E80-\u9FFF\uF900-\uFAFF\u3000-\u303F\uFF00-\uFFEF]/.test(ch);
}

function textWidth(text, fontSize) {
  let width = 0;
  for (const ch of String(text || "")) {
    width += charWidth(ch, fontSize);
  }
  return width;
}

function unbreakableTextWidth(text, fontSize) {
  let longest = 0;
  let latinRun = 0;
  const flushRun = () => {
    longest = Math.max(longest, latinRun);
    latinRun = 0;
  };

  for (const ch of String(text || "")) {
    if (isCjkChar(ch)) {
      flushRun();
      longest = Math.max(longest, charWidth(ch, fontSize));
    } else if (/[\s、，。；：！？（）()[\]{}<>《》「」『』,.;:!?/\\|+\-=]+/.test(ch)) {
      flushRun();
    } else {
      latinRun += charWidth(ch, fontSize);
    }
  }
  flushRun();
  return longest;
}

function tableColumnWidths(rows, width = ANSWER_WIDTH) {
  const columnCount = Math.max(1, ...rows.map((row) => row.cells.length));
  const minWidths = Array(columnCount).fill(0);
  const preferredWidths = Array(columnCount).fill(0);

  for (const row of rows) {
    for (let index = 0; index < columnCount; index += 1) {
      const cell = row.cells[index];
      const text = inlineText(cell?.children);
      const headerSafety = cell?.header ? 20 : 0;
      const minContent = unbreakableTextWidth(text, TABLE_FONT_SIZE) + TABLE_CELL_PADDING_X + headerSafety;
      const preferredContent = textWidth(text, TABLE_FONT_SIZE) + TABLE_CELL_PADDING_X + headerSafety;
      minWidths[index] = Math.max(minWidths[index], Math.ceil(minContent));
      preferredWidths[index] = Math.max(preferredWidths[index], Math.ceil(preferredContent));
    }
  }

  const minimumColumnWidth = columnCount >= 6 ? 48 : 58;
  for (let index = 0; index < columnCount; index += 1) {
    minWidths[index] = Math.max(minimumColumnWidth, minWidths[index]);
    preferredWidths[index] = Math.max(minWidths[index], preferredWidths[index]);
  }

  const minTotal = minWidths.reduce((sum, item) => sum + item, 0);
  if (minTotal >= width) {
    return minWidths.map((item) => Math.max(1, (item / minTotal) * width));
  }

  const preferredTotal = preferredWidths.reduce((sum, item) => sum + item, 0);
  if (preferredTotal <= width) {
    const remaining = width - preferredTotal;
    return preferredWidths.map((item) => item + remaining / columnCount);
  }

  const remaining = width - minTotal;
  return minWidths.map((minWidth, index) => minWidth + remaining * (preferredWidths[index] / preferredTotal));
}

function renderTable(tokens, start, end, pattern, keyPrefix) {
  const rows = collectTableRows(tokens, start, end);
  const columnWidths = tableColumnWidths(rows, ANSWER_WIDTH);

  return h(
    "div",
    {
      key: keyPrefix,
      style: {
        display: "flex",
        flexDirection: "column",
        width: "100%",
        marginBottom: 16,
      },
    },
    rows.map((row, rowIndex) =>
      h(
        "div",
        {
          key: `${keyPrefix}-r-${rowIndex}`,
          style: {
            display: "flex",
            flexDirection: "row",
            borderBottom: "1px solid rgba(226, 232, 240, 0.8)",
          },
        },
        row.cells.map((cell, cellIndex) =>
          h(
            "div",
            {
              key: `${keyPrefix}-r-${rowIndex}-c-${cellIndex}`,
              style: {
                display: "flex",
                flexWrap: "wrap",
                width: columnWidths[cellIndex],
                flexGrow: 0,
                flexShrink: 0,
                minWidth: 0,
                padding: "8px 12px",
                ...baseTextStyle({
                  fontSize: TABLE_FONT_SIZE,
                  lineHeight: TABLE_LINE_HEIGHT,
                  color: cell.header ? TEXT_DARK : TEXT_BODY,
                  fontWeight: cell.header ? 600 : 400,
                }),
              },
            },
            renderInline(cell.children, pattern, `${keyPrefix}-r-${rowIndex}-c-${cellIndex}`),
          ),
        ),
      ),
    ),
  );
}

function estimateTableHeight(tokens, start, end, width) {
  const rows = collectTableRows(tokens, start, end);
  if (!rows.length) return 0;

  const columnWidths = tableColumnWidths(rows, width);
  let height = 0;

  for (const row of rows) {
    let rowHeight = TABLE_MIN_ROW_HEIGHT;
    for (let cellIndex = 0; cellIndex < row.cells.length; cellIndex += 1) {
      const cell = row.cells[cellIndex];
      const cellWidth = Math.max(24, (columnWidths[cellIndex] || width / row.cells.length) - TABLE_CELL_PADDING_X);
      const text = inlineText(cell.children);
      const lines = estimateLines(text, cellWidth, TABLE_FONT_SIZE);
      rowHeight = Math.max(rowHeight, lines * TABLE_FONT_SIZE * TABLE_LINE_HEIGHT + TABLE_CELL_PADDING_Y);
    }
    height += rowHeight;
  }

  return height + 16;
}

function inlineText(token) {
  if (!token?.children) return "";
  return token.children
    .filter((child) => child.type !== "image")
    .map((child) => child.content || "")
    .join("");
}

function charWidth(ch, fontSize) {
  if (/[\u2E80-\u9FFF\uF900-\uFAFF\u3000-\u303F\uFF00-\uFFEF]/.test(ch)) return fontSize;
  if (/\s/.test(ch)) return fontSize * 0.35;
  if (/[A-Z0-9]/.test(ch)) return fontSize * 0.62;
  return fontSize * 0.54;
}

function estimateLines(text, availableWidth, fontSize) {
  let current = 0;
  let lines = 1;
  for (const ch of String(text || "")) {
    if (ch === "\n") {
      lines += 1;
      current = 0;
      continue;
    }
    const w = charWidth(ch, fontSize);
    if (current + w > availableWidth && current > 0) {
      lines += 1;
      current = w;
    } else {
      current += w;
    }
  }
  return Math.max(1, lines);
}

function estimateBlocks(tokens, start = 0, end = tokens.length, width = ANSWER_WIDTH) {
  let height = 0;
  let hasRenderedBlock = false;
  for (let i = start; i < end; i += 1) {
    const token = tokens[i];
    if (token.type === "heading_open") {
      const level = Number(String(token.tag || "h3").slice(1)) || 3;
      const metrics = headingMetrics(level, { firstBlock: !hasRenderedBlock });
      height += estimateLines(inlineText(tokens[i + 1]), width, metrics.fontSize) * metrics.fontSize * metrics.lineHeight + metrics.marginTop + metrics.marginBottom;
      hasRenderedBlock = true;
      i += 2;
    } else if (token.type === "paragraph_open") {
      height += estimateLines(inlineText(tokens[i + 1]), width, 15) * 15 * 1.75 + 16;
      hasRenderedBlock = true;
      i += 2;
    } else if (token.type === "bullet_list_open" || token.type === "ordered_list_open") {
      const close = findMatchingBlockClose(tokens, i, token.type, token.type === "bullet_list_open" ? "bullet_list_close" : "ordered_list_close");
      height += estimateBlocks(tokens, i + 1, close, width - 24) + 8;
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "list_item_open") {
      const close = findMatchingBlockClose(tokens, i, "list_item_open", "list_item_close");
      height += estimateBlocks(tokens, i + 1, close, width) + 6;
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "blockquote_open") {
      const close = findMatchingBlockClose(tokens, i, "blockquote_open", "blockquote_close");
      height += estimateBlocks(tokens, i + 1, close, width - 20) + 12;
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "fence" || token.type === "code_block") {
      const lines = String(token.content || "").split(/\r?\n/).length;
      height += Math.max(1, lines) * 13 * 1.55 + 48;
      hasRenderedBlock = true;
    } else if (token.type === "table_open") {
      const close = findMatchingBlockClose(tokens, i, "table_open", "table_close");
      height += estimateTableHeight(tokens, i + 1, close, width);
      hasRenderedBlock = true;
      i = close;
    } else if (token.type === "hr") {
      height += 17;
      hasRenderedBlock = true;
    } else if (token.type === "html_block") {
      height += estimateLines(token.content, width, 15) * 15 * 1.75 + 16;
      hasRenderedBlock = true;
    }
  }
  return height;
}

function estimateCanvasHeightFromAnswerHeight(answerHeight, keyword, includeBadges) {
  const headerHeight = 32 + 24 + 40;
  const footerHeight = 45;
  const chatMarginBottom = 40;
  const cardPaddingY = 64 + 48;
  const keywordText = String(keyword || "").trim();
  const keywordHeight = includeBadges && keywordText ? estimateLines(keywordText, CARD_WIDTH - CARD_PADDING_X * 2, 15) * 15 * 1.6 + 24 + 28 : 0;
  const messageHeight = answerHeight;
  const cardHeight = cardPaddingY + headerHeight + keywordHeight + messageHeight + chatMarginBottom + footerHeight;
  return Math.max(MIN_VIEWPORT_HEIGHT, Math.min(22000, Math.ceil((BODY_PADDING_TOP + cardHeight + BODY_PADDING_BOTTOM + 120) * 1.12)));
}

function buildLogo(platform) {
  if (typeof platform === "object" && platform?.logoDataUri) {
    return h("img", {
      src: platform.logoDataUri,
      style: {
        width: 28,
        height: 28,
        objectFit: "contain",
      },
    });
  }
  if (typeof platform === "object") {
    platform = platform.platform;
  }
  const uri = dataUriForLogo(platform);
  if (uri) {
    return h("img", {
      src: uri,
      style: {
        width: 28,
        height: 28,
        objectFit: "contain",
      },
    });
  }
  const label = platformLabels[platform] || platform || "AI";
  return h(
    "div",
    {
      style: {
        fontFamily: "Source Han Sans CN",
        fontSize: 13,
        fontWeight: 500,
        color: "#3b82f6",
      },
    },
    String(label).slice(0, 2).toUpperCase(),
  );
}

function buildTree(payload, answerContent, height) {
  const keyword = String(payload.keyword || "").trim();
  const includeBadges = payload.includeBadges !== false;
  const platform = String(payload.platform || "AI");

  const keywordSection = includeBadges && keyword
    ? h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "row-reverse",
            marginBottom: 28,
          },
        },
        h(
          "div",
          {
            style: {
              maxWidth: "80%",
              padding: "12px 20px",
              borderRadius: 20,
              backgroundColor: TEXT_DARK,
              color: "#f8fafc",
              fontFamily: "Source Han Sans CN",
              fontSize: 15,
              lineHeight: 1.6,
              display: "flex",
              flexWrap: "wrap",
            },
          },
          keyword,
        ),
      )
    : null;

  return h(
    "div",
    {
      style: {
        width: WIDTH,
        height,
        backgroundColor: "transparent",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: `${BODY_PADDING_TOP}px 20px ${BODY_PADDING_BOTTOM}px`,
        fontFamily: "Source Han Sans CN",
      },
    },
    h(
      "div",
      {
        style: {
          width: CARD_WIDTH,
          backgroundColor: "rgba(255, 255, 255, 0.6)",
          border: "1px solid rgba(226, 232, 240, 0.6)",
          borderRadius: 32,
          padding: "64px 48px 48px",
          boxShadow: "0 30px 60px -15px rgba(0, 0, 0, 0.05), inset 0 0 0 1px rgba(255, 255, 255, 0.5)",
          display: "flex",
          flexDirection: "column",
        },
      },
      h(
        "div",
        {
          style: {
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            paddingBottom: 24,
            marginBottom: 40,
            borderBottom: "1px solid rgba(226, 232, 240, 0.6)",
          },
        },
        h(
          "div",
          {
            style: {
              display: "flex",
              fontFamily: "Newsreader",
              fontSize: 32,
              fontWeight: 400,
              color: TEXT_DARK,
              letterSpacing: 0,
            },
          },
          "Surfaced",
          h("span", { style: { color: CYAN } }, "."),
        ),
        h(
          "div",
          {
            style: {
              fontFamily: "Inter",
              fontSize: 12,
              color: TEXT_LIGHT,
              fontWeight: 500,
              letterSpacing: 1.1,
              textTransform: "uppercase",
              marginBottom: 4,
            },
          },
          "THREAD SNAPSHOT",
        ),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "column",
            marginBottom: 40,
          },
        },
        keywordSection,
        h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "row",
              alignItems: "flex-start",
              gap: 16,
            },
          },
          h(
            "div",
            {
              style: {
                width: 44,
                height: 44,
                borderRadius: 12,
                backgroundColor: "#f8fafc",
                border: "1px solid rgba(226, 232, 240, 0.8)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              },
            },
            buildLogo({ platform, logoDataUri: payload.logoDataUri }),
          ),
          h(
            "div",
            {
              style: {
                width: ANSWER_WIDTH,
                display: "flex",
                flexDirection: "column",
                color: TEXT_BODY,
                fontFamily: "Source Han Sans CN",
                fontSize: 15,
                lineHeight: 1.75,
                letterSpacing: 0,
              },
            },
            answerContent,
          ),
        ),
      ),
      h(
        "div",
        {
          style: {
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            paddingTop: 32,
            borderTop: "1px solid rgba(226, 232, 240, 0.6)",
          },
        },
        h(
          "div",
          {
            style: {
              fontFamily: "JetBrains Mono",
              fontSize: 12,
              color: TEXT_LIGHT,
              letterSpacing: 0.6,
            },
          },
          payload.timestamp || "",
        ),
        h(
          "div",
          {
            style: {
              fontFamily: "Inter",
              fontSize: 12,
              fontWeight: 600,
              color: TEXT_LIGHT,
              letterSpacing: 1.2,
              textTransform: "uppercase",
            },
          },
          "AI MONITORING",
        ),
      ),
    ),
  );
}

async function renderText(payload) {
  if (!fonts.length) {
    throw new Error("No fonts loaded for Satori renderer.");
  }
  const text = normalizeCopiedMarkdownArtifacts(payload.text);
  const pattern = compileBrandPattern(payload.brand);
  const tokens = md.render(text).trim() ? md.parse(text, {}) : md.parse(" ", {});
  const answerContent = renderBlocks(tokens, 0, tokens.length, pattern, "body");
  const answerHeight = Math.max(44, estimateBlocks(tokens));
  const height = estimateCanvasHeightFromAnswerHeight(answerHeight, payload.keyword, payload.includeBadges !== false);
  const tree = buildTree(payload, answerContent, height);
  const svg = await satori(tree, {
    width: WIDTH,
    height,
    fonts,
    loadAdditionalAsset: async (code, segment) => {
      if (code !== "emoji") return [];
      return emojiImageDataUri(segment) || [];
    },
  });
  const scale = resolveRasterScale(payload.scale, height);
  const pngData = await rasterizeSvg(svg, scale);
  fs.writeFileSync(payload.outputPath, pngData);
  return { outputPath: payload.outputPath, width: Math.round(WIDTH * scale), height: Math.round(height * scale), scale };
}

function resolveRasterScale(requestedScale, height) {
  const parsed = Number(requestedScale);
  const desired = Number.isFinite(parsed) ? Math.min(3, Math.max(1, parsed)) : 2;
  return Math.max(1, Math.min(desired, MAX_RASTER_HEIGHT / Math.max(1, height)));
}

async function rasterizeSvg(svg, scale) {
  try {
    return await sharp(Buffer.from(svg), { density: BASE_DENSITY * scale }).png().toBuffer();
  } catch (sharpError) {
    const resvg = new Resvg(svg, {
      fitTo: {
        mode: "width",
        value: Math.round(WIDTH * scale),
      },
    });
    try {
      return resvg.render().asPng();
    } catch (resvgError) {
      throw new Error(`SVG rasterization failed: sharp=${sharpError.message}; resvg=${resvgError.message}`);
    }
  }
}

async function handleRequest(request) {
  if (request.type !== "renderText") {
    throw new Error(`Unsupported request type: ${request.type}`);
  }
  if (!request.outputPath) {
    throw new Error("Missing outputPath.");
  }
  fs.mkdirSync(path.dirname(request.outputPath), { recursive: true });
  return renderText(request);
}

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

for await (const line of rl) {
  if (!line.trim()) continue;
  let request;
  try {
    request = JSON.parse(line);
    const result = await handleRequest(request);
    process.stdout.write(JSON.stringify({ id: request.id, ok: true, result }) + "\n");
  } catch (error) {
    process.stdout.write(
      JSON.stringify({
        id: request?.id,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }) + "\n",
    );
  }
}
