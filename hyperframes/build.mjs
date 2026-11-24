// Usage: node build.mjs <plan.json> <compositionDir>
// Generates a HyperFrames composition (index.html + local assets) from a LiveForge edit plan.
// Video only: audio (source + sfx) is mixed by render_hf.py with ffmpeg.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const fontkit = require("fontkit");
const here = path.dirname(fileURLToPath(import.meta.url));
const [planPath, outDir] = process.argv.slice(2);
if (!planPath || !outDir) throw new Error("usage: node build.mjs <plan.json> <compositionDir>");

const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const { width: W, height: H } = plan.canvas;
const [SW, SH] = plan.source_size;
const DUR = plan.duration_ms / 1000;
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const f3 = (n) => +n.toFixed(3);

// ---- assets (everything local, copied next to index.html)
fs.mkdirSync(outDir, { recursive: true });
const anton = path.join(here, "..", "assets", "fonts", "Anton-Regular.ttf");
const fontSrc = fs.existsSync(anton) ? anton : "C:/Windows/Fonts/impact.ttf";
fs.copyFileSync(fontSrc, path.join(outDir, "font.ttf"));
fs.copyFileSync(path.join(here, "node_modules", "gsap", "dist", "gsap.min.js"), path.join(outDir, "gsap.min.js"));
const srcExt = path.extname(plan.source) || ".mp4";
fs.copyFileSync(plan.source, path.join(outDir, "source" + srcExt));
const font = fontkit.openSync(fontSrc);
const textW = (s, px) => (font.layout(s).advanceWidth / font.unitsPerEm) * px;

// ---- layout: rects in canvas px come from plan.layout
const L = plan.layout;
const rectOf = (p) => [p.x, p.y, p.w, p.h];

// A <video> of the full source, positioned so source region `box` fills `rect` (cover) or fits inside it (contain)
function panel(id, box, rect, cover, extra = "") {
  const [x, y, w, h] = box;
  const [rx, ry, rw, rh] = rect;
  const s = cover ? Math.max(rw / w, rh / h) : Math.min(rw / w, rh / h);
  const left = rw / 2 - (x + w / 2) * s;
  const top = rh / 2 - (y + h / 2) * s;
  return `<div class="panel" style="left:${f3(rx)}px;top:${f3(ry)}px;width:${f3(rw)}px;height:${f3(rh)}px">` +
    `<video id="${id}" class="clip" src="source${srcExt}" data-start="0" data-duration="${DUR}" muted playsinline ` +
    `style="left:${f3(left)}px;top:${f3(top)}px;width:${f3(SW * s)}px;height:${f3(SH * s)}px;${extra}"></video></div>`;
}
const { blur = 24, dim = 0.35 } = L.background || {};
const pad = blur * 2; // bleed past the canvas so the blur does not darken the edges
let stage = panel("bg", [0, 0, SW, SH], [-pad, -pad, W + 2 * pad, H + 2 * pad], true,
  `filter:${blur ? `blur(${blur}px) ` : ""}brightness(${f3(1 - dim)})`);
if (L.game.visible !== false) stage += panel("game", plan.game_box, rectOf(L.game), L.game.fit === "cover");
if (L.cam.visible !== false && plan.webcam_box) stage += panel("cam", plan.webcam_box, rectOf(L.cam), L.cam.fit === "cover");

// ---- captions
const color = L.caption.color;
const capY = L.caption.y * H;
const maxW = W * 0.82; // 9% side margins
const basePx = L.caption.size;
const POP = 1.15;

function layoutGroup(words) {
  for (let px = basePx; ; px -= 2) {
    const lines = [[]];
    let cur = 0;
    for (const w of words) {
      const ww = textW(w.text.toUpperCase(), px) * POP + 18 + 0.2 * px; // pop + stroke headroom + word margins
      const sp = textW(" ", px);
      if (lines.at(-1).length && cur + sp + ww > maxW) { lines.push([]); cur = 0; }
      cur += (lines.at(-1).length ? sp : 0) + ww;
      lines.at(-1).push(w);
    }
    if (lines.length <= 2 || px <= 36) return { px, lines };
  }
}

const tweens = [];
let capHtml = "";
plan.captions.forEach((g, gi) => {
  if (g.end_ms - g.start_ms < 50) return; // a zero-length clip would stay on screen until the end
  const { px, lines } = layoutGroup(g.words);
  let wi = 0;
  const body = lines.map((l) => `<div class="ln">${l.map((w) => {
    const id = `w${gi}_${wi++}`;
    tweens.push(`tl.fromTo("#${id}",{scale:1},{scale:${POP},duration:0.1,ease:"power2.out"},${f3(w.start_ms / 1000)});`);
    tweens.push(`tl.to("#${id}",{scale:1,duration:0.1,ease:"power2.in"},${f3(w.end_ms / 1000)});`);
    return `<span class="w" id="${id}">${esc(w.text.toUpperCase())}</span>`;
  }).join(" ")}</div>`).join("");
  tweens.push(`tl.fromTo("#g${gi}",{scale:0.8,opacity:0},{scale:1,opacity:1,duration:0.12,ease:"back.out(2)"},${f3(g.start_ms / 1000)});`);
  capHtml += `<div id="g${gi}" class="clip cap" data-start="${f3(g.start_ms / 1000)}" data-duration="${f3((g.end_ms - g.start_ms) / 1000)}" ` +
    `style="top:${f3(capY)}px;height:0;font-size:${px}px">${body}</div>\n`;
});

// ---- hook text
let hookHtml = "";
if (plan.hook_text) {
  const hy = L.hook.y * H;
  hookHtml = `<div id="hook" class="clip" data-start="0" data-duration="1.8" style="top:${f3(hy)}px;height:0">${esc(plan.hook_text.toUpperCase())}</div>`;
  tweens.push(`tl.fromTo("#hook",{scale:0.6,opacity:0,y:-40},{scale:1,opacity:1,y:0,duration:0.25,ease:"back.out(2)"},0.05);`);
  tweens.push(`tl.to("#hook",{scale:0.9,opacity:0,y:-30,duration:0.2,ease:"power2.in"},1.55);`);
}

// ---- zoom punches (whole stage, not captions): in over 35%, out over 65%
const zooms = (plan.effects || []).filter((e) => e.type === "zoom");
zooms.forEach((z) => {
  const t = z.start_ms / 1000, d = z.duration_ms / 1000;
  const [ax, ay] = z.anchor || [0.5, 0.5];
  tweens.push(`tl.set("#stage",{transformOrigin:"${f3(ax * W)}px ${f3(ay * H)}px"},${f3(t)});`);
  tweens.push(`tl.fromTo("#stage",{scale:1},{scale:${z.scale},duration:${f3(d * 0.35)},ease:"power2.out"},${f3(t)});`);
  tweens.push(`tl.to("#stage",{scale:1,duration:${f3(d * 0.65)},ease:"power2.inOut"},${f3(t + d * 0.35)});`);
});

const html = `<!doctype html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=${W}, height=${H}">
<script src="gsap.min.js"></script>
<style>
@font-face{font-family:"CapFont";src:url("font.ttf")}
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:${W}px;height:${H}px;overflow:hidden;background:#000}
#root{position:relative;width:${W}px;height:${H}px;overflow:hidden;font-family:"CapFont",Impact,sans-serif}
#stage{position:absolute;left:0;top:0;width:${W}px;height:${H}px}
.panel{position:absolute;overflow:hidden}
.panel video{position:absolute;max-width:none}
.cap{position:absolute;left:0;width:${W}px;height:0;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;line-height:1.12;color:${color}}
.ln{white-space:nowrap}
.w{display:inline-block;margin:0 .1em;-webkit-text-stroke:${Math.round(basePx * 0.22)}px #000;paint-order:stroke fill;stroke-linejoin:round;text-shadow:0 8px 0 rgba(0,0,0,.55),0 10px 14px rgba(0,0,0,.6)}
#hook{position:absolute;left:0;width:${W}px;height:0;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;line-height:1.12;font-size:${L.hook.size}px;color:${L.hook.color};-webkit-text-stroke:16px #000;paint-order:stroke fill;stroke-linejoin:round;text-shadow:0 6px 0 rgba(0,0,0,.55)}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="${DUR}" data-width="${W}" data-height="${H}">
<div id="stage">${stage}</div>
${capHtml}${hookHtml}
</div>
<script>
window.__timelines = window.__timelines || {};
const tl = gsap.timeline({ paused: true });
${tweens.join("\n")}
window.__timelines["main"] = tl;
</script></body></html>
`;
fs.writeFileSync(path.join(outDir, "index.html"), html);
fs.writeFileSync(path.join(outDir, "meta.json"), JSON.stringify({ id: "liveforge", name: "liveforge" }));
