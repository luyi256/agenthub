import * as esbuild from "esbuild";
import process from "node:process";

const watch = process.argv.includes("--watch");
const context = await esbuild.context({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  external: ["vscode"],
  platform: "node",
  format: "cjs",
  target: "node20",
  sourcemap: true,
  logLevel: "info"
});

if (watch) {
  await context.watch();
  console.log("Agent Hub extension is watching.");
} else {
  await context.rebuild();
  await context.dispose();
}
