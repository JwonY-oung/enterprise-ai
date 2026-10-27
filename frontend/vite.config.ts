import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    target: "esnext"
  },
  server: {
    proxy: {
      "/api": process.env.VITE_API_PROXY || "http://127.0.0.1:8000",
      "/health": process.env.VITE_API_PROXY || "http://127.0.0.1:8000"
    }
  }
});
