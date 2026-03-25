import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { copyFileSync, mkdirSync, existsSync } from "fs";

// Plugin to copy manifest.json and assets to dist
function copyExtensionFiles() {
  return {
    name: "copy-extension-files",
    closeBundle() {
      const distDir = resolve(__dirname, "dist");
      // Copy manifest
      copyFileSync(
        resolve(__dirname, "manifest.json"),
        resolve(distDir, "manifest.json")
      );
      // Copy assets (icons)
      const assetsSrc = resolve(__dirname, "assets");
      const assetsDst = resolve(distDir, "assets");
      if (!existsSync(assetsDst)) mkdirSync(assetsDst, { recursive: true });
      const icons = ["icon-16.png", "icon-48.png", "icon-128.png"];
      for (const icon of icons) {
        const src = resolve(assetsSrc, icon);
        if (existsSync(src)) {
          copyFileSync(src, resolve(assetsDst, icon));
        }
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), copyExtensionFiles()],
  root: resolve(__dirname, "src"),
  build: {
    outDir: resolve(__dirname, "dist"),
    emptyOutDir: true,
    rollupOptions: {
      input: {
        popup: resolve(__dirname, "src/popup/index.html"),
        content: resolve(__dirname, "src/content/mount.ts"),
        background: resolve(__dirname, "src/background/service-worker.ts"),
      },
      output: {
        entryFileNames: (chunk) => {
          if (chunk.name === "background") return "background/service-worker.js";
          if (chunk.name === "content") return "content/mount.js";
          return "popup/[name].js";
        },
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
