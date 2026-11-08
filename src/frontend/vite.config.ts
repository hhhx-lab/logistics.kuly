import { fileURLToPath } from "node:url";
import { uiLocalizationPlugin } from "./scripts/ui-localization-plugin.mjs";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const projectRoot = fileURLToPath(new URL("../../", import.meta.url));

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, projectRoot, "VITE_");
  const target = env.VITE_API_PROXY_TARGET;

  return {
    plugins: [uiLocalizationPlugin(), react()],
    envDir: projectRoot,
    server: {
      proxy: target ? { "/api": { target, changeOrigin: true } } : undefined,
    },
  };
});
