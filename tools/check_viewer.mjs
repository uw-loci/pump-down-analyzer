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
if (/Math\.(?:min|max)\(\.\.\./.test(scripts[2])) {
  throw new Error("Viewer uses spread extrema, which fails for large pressure series");
}
if (!Array.isArray(data.source_logs) || data.source_logs.length < 1) {
  throw new Error("Viewer payload is missing its source log manifest");
}
if (data.sample_meta.some((row) => !Array.isArray(row) || row.length < 4)) {
  throw new Error("Viewer sample metadata is missing source-log provenance");
}
console.log(
  JSON.stringify({
    scripts: scripts.length,
    bytes: Buffer.byteLength(html),
    readings: data.readings.length,
    states: data.state_names.length,
    intervals: data.active_state_intervals.length,
    changes: data.state_changes.length,
    sourceLogs: data.source_logs.length,
  }),
);
