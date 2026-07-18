import fs from "node:fs";

const viewerPath = process.argv[2];
if (!viewerPath) {
  throw new Error("Usage: node tools/check_viewer.mjs <viewer.html>");
}

const html = fs.readFileSync(viewerPath, "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)].map(
  (match) => match[1],
);
if (scripts.length !== 3) {
  throw new Error(`Expected three script elements, found ${scripts.length}`);
}
const data = JSON.parse(scripts[0]);
JSON.parse(scripts[1]);
new Function(scripts[2]);
console.log(
  JSON.stringify({
    scripts: scripts.length,
    bytes: Buffer.byteLength(html),
    readings: data.readings.length,
    states: data.state_names.length,
    intervals: data.active_state_intervals.length,
    changes: data.state_changes.length,
  }),
);
